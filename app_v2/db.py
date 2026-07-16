"""SQLite persistence layer for experiments and exercises.

Schema
------
experiments(id, patient_number, height, age, weight, properties, distance_meters, created_at)
exercises(id, experiment_id, created_at, recording_status, recording_started_at,
          recording_ended_at, wav_filename, notes, properties)
sensor_blobs(id, exercise_id, sensor_type, csv_gz)   -- zlib-compressed CSV bytes
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "experiments.db"

_CREATE_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS experiments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_number   TEXT,
    height           REAL,
    age              INTEGER,
    weight           REAL,
    properties       TEXT    DEFAULT '{}',
    distance_meters  REAL,
    created_at       REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS exercises (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id        INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    created_at           REAL    NOT NULL,
    recording_status     TEXT    NOT NULL DEFAULT 'idle',
    recording_started_at REAL,
    recording_ended_at   REAL,
    wav_filename         TEXT,
    notes                TEXT,
    properties           TEXT    DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sensor_blobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
    sensor_type TEXT    NOT NULL,
    csv_gz      BLOB    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ex_exp ON exercises(experiment_id);
CREATE INDEX IF NOT EXISTS idx_sb_ex  ON sensor_blobs(exercise_id);
"""

_MIGRATIONS = [
    # v1→v2: new experiment fields
    "ALTER TABLE experiments ADD COLUMN patient_number TEXT",
    "ALTER TABLE experiments ADD COLUMN height REAL",
    "ALTER TABLE experiments ADD COLUMN age INTEGER",
    "ALTER TABLE experiments ADD COLUMN weight REAL",
    "ALTER TABLE experiments ADD COLUMN properties TEXT DEFAULT '{}'",
    # v1→v2: distance was already there (skip if exists)
    "ALTER TABLE experiments ADD COLUMN distance_meters REAL",
    # v1→v2: new exercise columns
    "ALTER TABLE exercises ADD COLUMN created_at REAL",
    "ALTER TABLE exercises ADD COLUMN recording_status TEXT DEFAULT 'idle'",
    "ALTER TABLE exercises ADD COLUMN recording_started_at REAL",
    "ALTER TABLE exercises ADD COLUMN recording_ended_at REAL",
    "ALTER TABLE exercises ADD COLUMN properties TEXT DEFAULT '{}'",
]

_DATA_MIGRATIONS = [
    # Preserve old name field into patient_number
    "UPDATE experiments SET patient_number=name WHERE patient_number IS NULL AND name IS NOT NULL",
    # Backfill exercise created_at from old started_at
    "UPDATE exercises SET created_at=started_at WHERE created_at IS NULL",
    # Backfill recording_started_at / recording_ended_at from old columns
    "UPDATE exercises SET recording_started_at=started_at WHERE recording_started_at IS NULL AND started_at IS NOT NULL",
    "UPDATE exercises SET recording_ended_at=stopped_at WHERE recording_ended_at IS NULL AND stopped_at IS NOT NULL",
    # Set stopped exercises to correct status
    "UPDATE exercises SET recording_status='stopped' WHERE recording_ended_at IS NOT NULL AND recording_status='idle'",
    # nhm→mar sensor type rename
    "UPDATE sensor_blobs SET sensor_type='mar' WHERE sensor_type='nhm'",
]


def init_db() -> None:
    with _conn() as cx:
        cx.executescript(_CREATE_SQL)
        for sql in _MIGRATIONS:
            try:
                cx.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists
        for sql in _DATA_MIGRATIONS:
            try:
                cx.execute(sql)
            except sqlite3.OperationalError:
                pass  # column may not exist on fresh DBs


@contextmanager
def _conn():
    cx = sqlite3.connect(DB_PATH, check_same_thread=False)
    cx.row_factory = sqlite3.Row
    try:
        yield cx
        cx.commit()
    except Exception:
        cx.rollback()
        raise
    finally:
        cx.close()


# ── Experiments ───────────────────────────────────────────────────────────────

def create_experiment(
    patient_number: Optional[str],
    height: Optional[float],
    age: Optional[int],
    weight: Optional[float],
    properties: dict,
    distance_meters: Optional[float],
    created_at: float,
) -> int:
    with _conn() as cx:
        cur = cx.execute(
            """INSERT INTO experiments
               (patient_number, height, age, weight, properties, distance_meters, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (patient_number, height, age, weight, json.dumps(properties), distance_meters, created_at),
        )
        return cur.lastrowid


def update_experiment(exp_id: int, **fields) -> None:
    allowed = {"patient_number", "height", "age", "weight", "properties", "distance_meters"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    if "properties" in updates:
        updates["properties"] = json.dumps(updates["properties"])
    cols = ", ".join(f"{k}=?" for k in updates)
    with _conn() as cx:
        cx.execute(f"UPDATE experiments SET {cols} WHERE id=?", (*updates.values(), exp_id))


def delete_experiment(exp_id: int) -> None:
    with _conn() as cx:
        cx.execute("PRAGMA foreign_keys=ON")
        cx.execute("DELETE FROM experiments WHERE id=?", (exp_id,))


def list_experiments(page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    offset = (page - 1) * page_size
    with _conn() as cx:
        total = cx.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        rows = cx.execute("""
            SELECT e.*, COUNT(x.id) AS exercise_count
            FROM   experiments e
            LEFT JOIN exercises x ON x.experiment_id = e.id
            GROUP BY e.id
            ORDER BY e.created_at DESC
            LIMIT ? OFFSET ?
        """, (page_size, offset)).fetchall()
    return [dict(r) for r in rows], total


def get_experiment(exp_id: int) -> Optional[dict]:
    with _conn() as cx:
        row = cx.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    return dict(row) if row else None


# ── Exercises ─────────────────────────────────────────────────────────────────

def create_exercise(experiment_id: int, properties: dict, created_at: float) -> int:
    with _conn() as cx:
        cur = cx.execute(
            "INSERT INTO exercises(experiment_id, created_at, properties) VALUES (?,?,?)",
            (experiment_id, created_at, json.dumps(properties)),
        )
        return cur.lastrowid


def start_exercise_recording(ex_id: int, started_at: float) -> None:
    with _conn() as cx:
        cx.execute(
            "UPDATE exercises SET recording_status='recording', recording_started_at=? WHERE id=?",
            (started_at, ex_id),
        )


def stop_exercise_recording(
    ex_id: int,
    ended_at: float,
    blobs: dict[str, bytes],
    wav_filename: Optional[str] = None,
) -> None:
    with _conn() as cx:
        cx.execute(
            "UPDATE exercises SET recording_status='stopped', recording_ended_at=?, wav_filename=? WHERE id=?",
            (ended_at, wav_filename, ex_id),
        )
        for sensor_type, gz_bytes in blobs.items():
            cx.execute(
                "INSERT INTO sensor_blobs(exercise_id, sensor_type, csv_gz) VALUES (?,?,?)",
                (ex_id, sensor_type, gz_bytes),
            )


def clear_exercise_data(ex_id: int) -> None:
    with _conn() as cx:
        cx.execute("PRAGMA foreign_keys=ON")
        cx.execute("DELETE FROM sensor_blobs WHERE exercise_id=?", (ex_id,))
        cx.execute(
            """UPDATE exercises SET
               recording_status='idle', recording_started_at=NULL,
               recording_ended_at=NULL, wav_filename=NULL
               WHERE id=?""",
            (ex_id,),
        )


def update_exercise_wav(ex_id: int, wav_filename: str) -> None:
    with _conn() as cx:
        cx.execute("UPDATE exercises SET wav_filename=? WHERE id=?", (wav_filename, ex_id))


def get_exercise(ex_id: int) -> Optional[dict]:
    with _conn() as cx:
        row = cx.execute("SELECT * FROM exercises WHERE id=?", (ex_id,)).fetchone()
    return dict(row) if row else None


def list_exercises_for_experiment(experiment_id: int) -> list[dict]:
    with _conn() as cx:
        rows = cx.execute(
            """
            SELECT x.*, GROUP_CONCAT(sb.sensor_type) AS sensors
            FROM   exercises x
            LEFT JOIN sensor_blobs sb ON sb.exercise_id = x.id
            WHERE  x.experiment_id = ?
            GROUP BY x.id
            ORDER BY x.created_at ASC
            """,
            (experiment_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_exercises_global(page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    offset = (page - 1) * page_size
    with _conn() as cx:
        total = cx.execute("SELECT COUNT(*) FROM exercises").fetchone()[0]
        rows = cx.execute(
            "SELECT * FROM exercises ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    return [dict(r) for r in rows], total


def delete_exercise(ex_id: int) -> None:
    with _conn() as cx:
        cx.execute("PRAGMA foreign_keys=ON")
        cx.execute("DELETE FROM exercises WHERE id=?", (ex_id,))


def get_sensor_blobs(ex_id: int) -> dict[str, bytes]:
    with _conn() as cx:
        rows = cx.execute(
            "SELECT sensor_type, csv_gz FROM sensor_blobs WHERE exercise_id=?", (ex_id,)
        ).fetchall()
    return {r["sensor_type"]: bytes(r["csv_gz"]) for r in rows}


# ── Legacy helpers (used by old /api/ dashboard endpoints) ────────────────────

def list_exercises(experiment_id: int) -> list[dict]:
    """Dashboard-facing: includes sensor type list per exercise."""
    with _conn() as cx:
        rows = cx.execute(
            """
            SELECT x.*, GROUP_CONCAT(sb.sensor_type) AS sensors
            FROM   exercises x
            LEFT JOIN sensor_blobs sb ON sb.exercise_id = x.id
            WHERE  x.experiment_id = ?
            GROUP BY x.id
            ORDER BY x.created_at ASC
            """,
            (experiment_id,),
        ).fetchall()
    return [dict(r) for r in rows]
