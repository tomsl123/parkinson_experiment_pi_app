"""Spec-compliant REST API matching app_v2/openapi.yaml.

Mounted at the root (no prefix) so paths match the spec exactly:
  /experiments, /exercises, /exercises/{id}/recording/start, etc.

IDs are stringified integers (spec requires string, doesn't mandate UUID).
Timestamps are ISO 8601 UTC strings.
"""
from __future__ import annotations

import io
import json
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Request, Response

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from app_v2 import db
from config import rms_to_dbspl

router = APIRouter()

RECORDINGS_DIR = Path(__file__).parent.parent / "recordings"


# ── Formatting helpers ────────────────────────────────────────────────────────

def _ts(unix: Optional[float]) -> Optional[str]:
    if unix is None:
        return None
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _experiment_out(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "patientNumber": row.get("patient_number"),
        "height": row.get("height"),
        "age": row.get("age"),
        "weight": row.get("weight"),
        "distanceMeters": row.get("distance_meters"),
        "properties": json.loads(row.get("properties") or "{}"),
        "createdAt": _ts(row["created_at"]),
    }


def _exercise_out(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "experimentId": str(row["experiment_id"]),
        "createdAt": _ts(row["created_at"]),
        "recordingStatus": row.get("recording_status") or "idle",
        "hasData": row.get("recording_ended_at") is not None,
        "recordingStartedAt": _ts(row.get("recording_started_at")),
        "recordingEndedAt": _ts(row.get("recording_ended_at")),
        "properties": json.loads(row.get("properties") or "{}"),
    }


def _err(msg: str) -> dict:
    return {"error": msg}


# ── Experiments ───────────────────────────────────────────────────────────────

@router.post("/experiments", status_code=201)
async def create_experiment(request: Request):
    body = await request.json()
    exp_id = db.create_experiment(
        patient_number=body.get("patientNumber"),
        height=body.get("height"),
        age=body.get("age"),
        weight=body.get("weight"),
        properties=body.get("properties") or {},
        distance_meters=body.get("distanceMeters"),
        created_at=time.time(),
    )
    return _experiment_out(db.get_experiment(exp_id))


@router.get("/experiments")
async def list_experiments(page: int = 1, pageSize: int = 20):
    page = max(1, page)
    pageSize = max(1, min(100, pageSize))
    rows, total = db.list_experiments(page, pageSize)
    return {"items": [_experiment_out(r) for r in rows], "page": page, "pageSize": pageSize, "total": total}


@router.get("/experiments/{experimentId}")
async def get_experiment(experimentId: str):
    exp = db.get_experiment(int(experimentId))
    if not exp:
        raise HTTPException(status_code=404, detail=_err("Experiment not found"))
    return _experiment_out(exp)


@router.patch("/experiments/{experimentId}")
async def update_experiment(experimentId: str, request: Request):
    exp = db.get_experiment(int(experimentId))
    if not exp:
        raise HTTPException(status_code=404, detail=_err("Experiment not found"))
    body = await request.json()
    updates = {}
    if "patientNumber" in body: updates["patient_number"]   = body["patientNumber"]
    if "height"        in body: updates["height"]            = body["height"]
    if "age"           in body: updates["age"]               = body["age"]
    if "weight"        in body: updates["weight"]            = body["weight"]
    if "properties"    in body: updates["properties"]        = body["properties"]
    if "distanceMeters" in body: updates["distance_meters"]  = body["distanceMeters"]
    db.update_experiment(int(experimentId), **updates)
    return _experiment_out(db.get_experiment(int(experimentId)))


@router.delete("/experiments/{experimentId}", status_code=204)
async def delete_experiment(experimentId: str):
    if not db.get_experiment(int(experimentId)):
        raise HTTPException(status_code=404, detail=_err("Experiment not found"))
    db.delete_experiment(int(experimentId))
    return Response(status_code=204)


# ── Exercises ─────────────────────────────────────────────────────────────────

@router.post("/experiments/{experimentId}/exercises", status_code=201)
async def create_exercise(experimentId: str, request: Request):
    exp = db.get_experiment(int(experimentId))
    if not exp:
        raise HTTPException(status_code=404, detail=_err("Experiment not found"))
    body = await request.json()
    ex_id = db.create_exercise(int(experimentId), body.get("properties") or {}, time.time())
    return _exercise_out(db.get_exercise(ex_id))


@router.get("/experiments/{experimentId}/exercises")
async def list_exercises_for_experiment(experimentId: str):
    if not db.get_experiment(int(experimentId)):
        raise HTTPException(status_code=404, detail=_err("Experiment not found"))
    rows = db.list_exercises_for_experiment(int(experimentId))
    return [_exercise_out(r) for r in rows]


@router.get("/exercises")
async def list_exercises_global(page: int = 1, pageSize: int = 20):
    page = max(1, page)
    pageSize = max(1, min(100, pageSize))
    rows, total = db.list_exercises_global(page, pageSize)
    return {"items": [_exercise_out(r) for r in rows], "page": page, "pageSize": pageSize, "total": total}


@router.get("/exercises/{exerciseId}")
async def get_exercise(exerciseId: str):
    ex = db.get_exercise(int(exerciseId))
    if not ex:
        raise HTTPException(status_code=404, detail=_err("Exercise not found"))
    return _exercise_out(ex)


@router.delete("/exercises/{exerciseId}", status_code=204)
async def delete_exercise(exerciseId: str):
    if not db.get_exercise(int(exerciseId)):
        raise HTTPException(status_code=404, detail=_err("Exercise not found"))
    db.delete_exercise(int(exerciseId))
    return Response(status_code=204)


# ── Recording ─────────────────────────────────────────────────────────────────

@router.post("/exercises/{exerciseId}/recording/start")
async def start_recording(exerciseId: str):
    from app_v2.server import _recording, _start_rec, _pi_ws, _broadcast_json

    ex = db.get_exercise(int(exerciseId))
    if not ex:
        raise HTTPException(status_code=404, detail=_err("Exercise not found"))
    if ex.get("recording_ended_at") is not None:
        raise HTTPException(status_code=409, detail=_err("Exercise already has data — clear it first"))
    if _recording:
        raise HTTPException(status_code=409, detail=_err("Another recording is already in progress"))

    _start_rec(int(exerciseId))
    if _pi_ws:
        import json as _json
        await _pi_ws.send_text(_json.dumps({"cmd": "start_wav"}))
    await _broadcast_json({"t": "rec_state", "recording": True, "exercise_id": int(exerciseId)})
    return _exercise_out(db.get_exercise(int(exerciseId)))


@router.post("/exercises/{exerciseId}/recording/stop")
async def stop_recording(exerciseId: str):
    from app_v2.server import _recording, _exercise_id, _stop_rec, _pi_ws, _broadcast_json

    ex = db.get_exercise(int(exerciseId))
    if not ex:
        raise HTTPException(status_code=404, detail=_err("Exercise not found"))
    if not _recording or _exercise_id != int(exerciseId):
        raise HTTPException(status_code=409, detail=_err("This exercise is not currently recording"))

    _stop_rec()
    if _pi_ws:
        import json as _json
        await _pi_ws.send_text(_json.dumps({"cmd": "stop_wav"}))
    await _broadcast_json({"t": "rec_state", "recording": False})
    return _exercise_out(db.get_exercise(int(exerciseId)))


# ── Data ──────────────────────────────────────────────────────────────────────

@router.get("/exercises/{exerciseId}/data")
async def get_exercise_data(exerciseId: str):
    ex = db.get_exercise(int(exerciseId))
    if not ex:
        raise HTTPException(status_code=404, detail=_err("Exercise not found"))
    if ex.get("recording_ended_at") is None:
        raise HTTPException(status_code=404, detail=_err("Exercise has no recorded data yet"))

    blobs = db.get_sensor_blobs(int(exerciseId))
    exp   = db.get_experiment(ex["experiment_id"])

    # ── mouthOpening ──────────────────────────────────────────────────────────
    mouth_values: list = []
    mouth_rate = 0.0
    mar_gz = blobs.get("mar")
    if mar_gz:
        mar_df = pd.read_csv(io.StringIO(zlib.decompress(mar_gz).decode()))
        if "vertical" in mar_df.columns and "horizontal" in mar_df.columns:
            mouth_values = mar_df[["vertical", "horizontal"]].values.tolist()
        elif "mar" in mar_df.columns:
            # legacy single-column blob: return [mar, 0] as placeholder
            mouth_values = [[v, 0.0] for v in mar_df["mar"].tolist()]
        diff = mar_df["timestamp"].diff().dropna()
        mouth_rate = float(1.0 / diff.median()) if len(diff) > 0 else 0.0

    # ── soundPressure ─────────────────────────────────────────────────────────
    spl_values: list = []
    spl_rate = 0.0
    audio_gz = blobs.get("audio")
    if audio_gz:
        spl_df = pd.read_csv(io.StringIO(zlib.decompress(audio_gz).decode()))
        spl_values = [rms_to_dbspl(v) for v in spl_df["spl"].tolist()]
        diff = spl_df["timestamp"].diff().dropna()
        spl_rate = float(1.0 / diff.median()) if len(diff) > 0 else 0.0

    # ── footSpeed + stepLengths via IMU step detection ────────────────────────
    foot_speeds: list = []
    step_lengths: list = []
    imu_rate = 0.0
    imu_gz = blobs.get("imu")
    if imu_gz:
        try:
            from feature_engineering import detect_steps
            imu_df = pd.read_csv(io.StringIO(zlib.decompress(imu_gz).decode()))
            # Auto-correct inverted Z axis: if mean accel_z is negative the sensor is
            # mounted upside-down (gravity points in -Z). Flip so detect_steps can find peaks.
            if imu_df["accel_z(g)"].mean() < 0:
                imu_df = imu_df.copy()
                imu_df["accel_z(g)"] = -imu_df["accel_z(g)"]
            step_ts, imu_rate = detect_steps(imu_df)
            duration_s = float(imu_df["timestamp"].iloc[-1] - imu_df["timestamp"].iloc[0])
            intervals = np.diff(step_ts).tolist()

            # step length: distance-based preferred, height-based fallback
            distance_m   = (exp or {}).get("distance_meters")
            height_cm    = (exp or {}).get("height")
            step_len_cm: Optional[float] = None
            if distance_m and len(step_ts) > 0:
                step_len_cm = (float(distance_m) * 100.0) / len(step_ts)
            elif height_cm:
                step_len_cm = 0.2075 * float(height_cm)

            if step_len_cm is not None and intervals:
                foot_speeds  = [round(step_len_cm / iv, 4) for iv in intervals if iv > 0]
                step_lengths = [round(step_len_cm, 4)] * len(step_ts)
        except Exception:
            pass

    # ── aggregates ────────────────────────────────────────────────────────────
    def _avg(vals):  return float(np.mean(vals))   if vals else None
    def _med(vals):  return float(np.median(vals)) if vals else None

    vert_vals = [v[0] for v in mouth_values] if mouth_values else []
    horiz_vals = [v[1] for v in mouth_values] if mouth_values else []

    aggregates = {
        "stepLengths": {"values": step_lengths, "unit": "cm"},
        "averages": {
            "mouthOpeningVertical":   _avg(vert_vals),
            "mouthOpeningHorizontal": _avg(horiz_vals),
            "soundPressure":          _avg(spl_values),
            "footSpeed":              _avg(foot_speeds),
            "stepLength":             float(step_lengths[0]) if step_lengths else None,
        },
        "medians": {
            "mouthOpeningVertical":   _med(vert_vals),
            "mouthOpeningHorizontal": _med(horiz_vals),
            "soundPressure":          _med(spl_values),
            "footSpeed":              _med(foot_speeds),
            "stepLength":             float(step_lengths[0]) if step_lengths else None,
        },
    }

    step_rate = float(len(step_lengths)) / duration_s if imu_gz and step_lengths else 0.0

    return {
        "exerciseId": str(exerciseId),
        "startedAt":  _ts(ex.get("recording_started_at")),
        "endedAt":    _ts(ex.get("recording_ended_at")),
        "mouthOpening":   {"values": mouth_values, "sampleRate": mouth_rate},
        "soundPressure":  {"values": spl_values,   "sampleRate": spl_rate,  "unit": "dBSPL"},
        "footSpeed":      {"values": foot_speeds,   "sampleRate": step_rate, "unit": "cm/s"},
        "aggregates":     aggregates,
    }


@router.delete("/exercises/{exerciseId}/data", status_code=204)
async def clear_exercise_data(exerciseId: str):
    ex = db.get_exercise(int(exerciseId))
    if not ex:
        raise HTTPException(status_code=404, detail=_err("Exercise not found"))
    db.clear_exercise_data(int(exerciseId))
    return Response(status_code=204)
