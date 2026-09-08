"""Pi Dashboard v2 — single FastAPI process, WebSocket push to browser.

Run (real Pi):
    uvicorn app_v2.server:app --host 0.0.0.0 --port 8080

Run (mock):
    MOCK=1 uvicorn app_v2.server:app --host 0.0.0.0 --port 8080 --reload

Pi streamer should point to:  ws://<server-ip>:8080/stream
"""
from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import math
import os
import random
import time
import zlib
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


import logging
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    ACCEL_SCALE, GYRO_SCALE, IMU_HZ, AUDIO_HZ, WINDOW_SECONDS,
    rms_to_dbspl, wav_to_dbspl_rows,
)
from server.buffer import SensorBuffer
from server.cv_processor import compute_nhm
from app_v2 import db

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Single-frame MAR processing: latest frame wins, stale frames dropped.
# Queue(1) holds at most one pending frame; putting a new frame drops the stale one.
_mar_queue: Optional[asyncio.Queue] = None

MOCK = os.getenv("MOCK", "0") == "1"

# Optional camera rotation applied to every frame before display and MAR processing.
# Set CAMERA_ROTATE=90CW, 90CCW, or 180 to correct a sideways/upside-down camera.
import cv2 as _cv2_global
_ROTATE_CODES = {
    "90CW":  _cv2_global.ROTATE_90_CLOCKWISE,
    "90CCW": _cv2_global.ROTATE_90_COUNTERCLOCKWISE,
    "180":   _cv2_global.ROTATE_180,
}
CAMERA_ROTATE_CODE = _ROTATE_CODES.get(os.getenv("CAMERA_ROTATE", ""))
RECORDINGS_DIR = Path(__file__).parent.parent / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)

_MAXLEN = WINDOW_SECONDS * max(IMU_HZ, AUDIO_HZ)
_buffer = SensorBuffer(maxlen=_MAXLEN)

_pi_ws: Optional[WebSocket] = None
_browser_clients: set[WebSocket] = set()

_recording        = False
_rec_rows: dict[str, list] = {}
_exercise_id:        Optional[int] = None  # current exercise being recorded
_latest_exercise_id: Optional[int] = None  # last completed exercise (for WAV association)
_latest_wav: Optional[Path] = None

app = FastAPI(title="Pi Dashboard v2")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ── Pi WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/stream")
async def pi_stream(ws: WebSocket):
    global _pi_ws
    await ws.accept()
    _pi_ws = ws
    await _broadcast_json({"t": "status", "connected": True})
    try:
        async for raw in ws.iter_text():
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _ingest(frame)
    except WebSocketDisconnect:
        pass
    finally:
        _pi_ws = None
        await _broadcast_json({"t": "status", "connected": False})


async def _mar_worker() -> None:
    """Single worker: always processes the latest camera frame, drops queued-up stale ones."""
    while True:
        try:
            jpeg, ts = await _mar_queue.get()
            result = await asyncio.get_event_loop().run_in_executor(None, compute_nhm, jpeg)
            if result is None:
                continue
            vertical, horizontal = result
            _buffer.push_nhm(ts, vertical / horizontal if horizontal > 1e-6 else None)
            await _broadcast_json({"t": "mar", "ts": ts, "vertical": vertical, "horizontal": horizontal})
            if _recording:
                _rec_rows.setdefault("mar", []).append([ts, vertical, horizontal])
        except Exception as e:
            log.warning("MAR worker error: %s", e, exc_info=True)


async def _ingest(frame: dict) -> None:
    ts = frame.get("ts", time.time())
    ftype = frame.get("type")

    if ftype == "imu":
        _buffer.push_imu(frame)
        msg = {
            "t": "imu", "ts": ts,
            "ax": frame.get("accel_x", 0), "ay": frame.get("accel_y", 0), "az": frame.get("accel_z", 0),
            "gx": frame.get("gyro_x",  0), "gy": frame.get("gyro_y",  0), "gz": frame.get("gyro_z",  0),
        }
        await _broadcast_json(msg)
        if _recording:
            _rec_rows.setdefault("imu", []).append([
                ts,
                frame.get("accel_x", 0) / ACCEL_SCALE,
                frame.get("accel_y", 0) / ACCEL_SCALE,
                frame.get("accel_z", 0) / ACCEL_SCALE,
                frame.get("gyro_x",  0) / GYRO_SCALE,
                frame.get("gyro_y",  0) / GYRO_SCALE,
                frame.get("gyro_z",  0) / GYRO_SCALE,
            ])

    elif ftype == "audio":
        _buffer.push_audio(frame)
        rms = frame.get("spl", 0)
        await _broadcast_json({"t": "audio", "ts": ts, "spl": rms_to_dbspl(rms)})
        if _recording:
            _rec_rows.setdefault("audio", []).append([ts, rms])  # store raw RMS

    elif ftype == "camera":
        try:
            jpeg = base64.b64decode(frame.get("jpeg", ""))
        except Exception:
            return
        # Decode, optionally rotate, fix R/B swap for browser display.
        try:
            import cv2 as _cv2
            import numpy as _np
            img = _cv2.imdecode(_np.frombuffer(jpeg, _np.uint8), _cv2.IMREAD_COLOR)
            if img is not None:
                if CAMERA_ROTATE_CODE is not None:
                    img = _cv2.rotate(img, CAMERA_ROTATE_CODE)
                # Re-encode corrected frame for browser (fix BGR→RGB swap) and for MAR
                _, buf = _cv2.imencode(".jpg", _cv2.cvtColor(img, _cv2.COLOR_BGR2RGB),
                                       [_cv2.IMWRITE_JPEG_QUALITY, 50])
                jpeg_display = bytes(buf)
                # MAR gets the rotated BGR jpeg (compute_nhm handles BGR→RGB internally)
                _, mar_buf = _cv2.imencode(".jpg", img, [_cv2.IMWRITE_JPEG_QUALITY, 80])
                jpeg_for_mar = bytes(mar_buf)
            else:
                jpeg_display = jpeg
                jpeg_for_mar = jpeg
        except Exception:
            jpeg_display = jpeg
            jpeg_for_mar = jpeg
        _buffer.push_camera(jpeg, ts)
        await _broadcast_bytes(b"CAM:" + jpeg_display)
        # Drop stale frame if queue is full, then enqueue latest
        if _mar_queue is not None:
            if _mar_queue.full():
                try:
                    _mar_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            _mar_queue.put_nowait((jpeg_for_mar, ts))

    elif ftype == "mar":
        vertical   = frame.get("vertical")
        horizontal = frame.get("horizontal")
        if vertical is not None and horizontal is not None:
            _buffer.push_nhm(ts, vertical / horizontal if horizontal > 1e-6 else None)
            await _broadcast_json({"t": "mar", "ts": ts, "vertical": vertical, "horizontal": horizontal})
            if _recording:
                _rec_rows.setdefault("mar", []).append([ts, vertical, horizontal])


# ── Browser WebSocket ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def browser_ws(ws: WebSocket):
    await ws.accept()
    _browser_clients.add(ws)
    await ws.send_text(json.dumps({"t": "status", "connected": _pi_ws is not None or MOCK}))
    await ws.send_text(json.dumps({"t": "rec_state", "recording": _recording}))
    try:
        async for raw in ws.iter_text():
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            cmd = msg.get("cmd")
            if cmd == "start_rec":
                _start_rec()
                await ws.send_text(json.dumps({"t": "rec_state", "recording": True}))
            elif cmd == "stop_rec":
                zip_bytes, filename = _stop_rec()
                await ws.send_bytes(b"ZIP:" + filename.encode().ljust(64) + zip_bytes)
                await ws.send_text(json.dumps({"t": "rec_state", "recording": False}))
            elif cmd in ("start_wav", "stop_wav"):
                if _pi_ws:
                    await _pi_ws.send_text(json.dumps({"cmd": cmd}))
    except WebSocketDisconnect:
        pass
    finally:
        _browser_clients.discard(ws)


# ── Broadcast helpers ─────────────────────────────────────────────────────────

async def _broadcast_json(msg: dict) -> None:
    global _browser_clients
    if not _browser_clients:
        return
    raw = json.dumps(msg)
    dead = set()
    for ws in _browser_clients:
        try:
            await ws.send_text(raw)
        except Exception:
            dead.add(ws)
    _browser_clients -= dead


async def _broadcast_bytes(data: bytes) -> None:
    global _browser_clients
    if not _browser_clients:
        return
    dead = set()
    for ws in _browser_clients:
        try:
            await ws.send_bytes(data)
        except Exception:
            dead.add(ws)
    _browser_clients -= dead


# ── Recording helpers ─────────────────────────────────────────────────────────

_CSV_HEADERS = {
    "imu":   ["timestamp", "accel_x(g)", "accel_y(g)", "accel_z(g)", "gyro_x(dps)", "gyro_y(dps)", "gyro_z(dps)"],
    "audio": ["timestamp", "spl"],
    "mar":   ["timestamp", "vertical", "horizontal"],
}


def _audio_csv_to_dbspl(raw_csv: str) -> str:
    """Re-encode a stored audio CSV (raw RMS) as dB SPL for export."""
    s_in  = io.StringIO(raw_csv)
    s_out = io.StringIO()
    reader = csv.reader(s_in)
    writer = csv.writer(s_out)
    next(reader, None)  # skip original header
    writer.writerow(["timestamp", "sound_pressure_dbspl"])
    for row in reader:
        if len(row) < 2:
            continue
        try:
            rms  = float(row[1])
            dbspl = round(rms_to_dbspl(rms), 4) if rms > 0 else ""
        except ValueError:
            dbspl = ""
        writer.writerow([row[0], dbspl])
    return s_out.getvalue()


def _audio_export_csv(raw_csv: str, ex: dict) -> str:
    """dB SPL for export. Prefer deriving from the WAV (high-passed, LF drift
    removed) since the stored per-block RMS has that drift baked in and cannot be
    corrected; fall back to re-encoding the stored RMS when no WAV is available."""
    wav = ex.get("wav_filename")
    if wav:
        wav_path = RECORDINGS_DIR / wav
        if wav_path.exists():
            try:
                rows = wav_to_dbspl_rows(str(wav_path))
                if rows:
                    s = io.StringIO()
                    w = csv.writer(s)
                    w.writerow(["timestamp", "sound_pressure_dbspl"])
                    w.writerows(rows)
                    return s.getvalue()
            except Exception:
                pass  # fall back to the stored-RMS conversion below
    return _audio_csv_to_dbspl(raw_csv)


def _build_csv(sensor: str, rows: list) -> str:
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(_CSV_HEADERS[sensor])
    w.writerows(rows)
    return s.getvalue()


def _build_blobs() -> dict[str, bytes]:
    return {
        sensor: zlib.compress(_build_csv(sensor, rows).encode())
        for sensor, rows in _rec_rows.items()
        if rows
    }


def _build_zip(ts_str: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sensor, rows in _rec_rows.items():
            if rows:
                zf.writestr(f"{sensor}_{ts_str}.csv", _build_csv(sensor, rows))
    buf.seek(0)
    return buf.read(), f"recording_{ts_str}.zip"


def _start_rec(exercise_id: int) -> None:
    global _recording, _rec_rows, _exercise_id
    _recording = True
    _rec_rows = {}
    _exercise_id = exercise_id
    db.start_exercise_recording(exercise_id, time.time())


def _stop_rec() -> tuple[bytes, str]:
    global _recording, _exercise_id, _latest_exercise_id
    _recording = False
    ts_str = time.strftime("%Y%m%d_%H%M%S")

    if _exercise_id is not None:
        db.stop_exercise_recording(_exercise_id, time.time(), _build_blobs())
        _latest_exercise_id = _exercise_id
        _exercise_id = None

    return _build_zip(ts_str)


# ── WAV upload (from Pi) ──────────────────────────────────────────────────────

@app.post("/wav")
async def post_wav(request: Request):
    global _latest_wav
    content_disposition = request.headers.get("content-disposition", "")
    filename = "audio_upload.wav"
    for part in content_disposition.split(";"):
        part = part.strip()
        if part.startswith("filename="):
            filename = part[len("filename="):].strip().strip('"')
            break

    wav_data = await request.body()
    dest = RECORDINGS_DIR / filename
    dest.write_bytes(wav_data)
    _latest_wav = dest

    if _latest_exercise_id is not None:
        db.update_exercise_wav(_latest_exercise_id, filename)

    return {"ok": True, "filename": filename, "bytes": len(wav_data)}


# ── Debug endpoints ───────────────────────────────────────────────────────────

@app.get("/debug/camera")
async def debug_camera():
    jpeg, ts = _buffer.get_camera()
    if jpeg is None:
        raise HTTPException(status_code=204, detail="No frame yet")
    return Response(content=jpeg, media_type="image/jpeg")


# ── Dashboard page routes ─────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def page_experiments(request: Request):
    return templates.TemplateResponse(request, "experiments.html")


@app.get("/dashboard/experiments/new", response_class=HTMLResponse)
async def page_experiment_new(request: Request):
    return templates.TemplateResponse(request, "experiment_new.html")


@app.get("/dashboard/experiments/{exp_id}", response_class=HTMLResponse)
async def page_experiment_detail(request: Request, exp_id: int):
    exp = db.get_experiment(exp_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    created_fmt = datetime.fromtimestamp(exp["created_at"]).strftime("%Y-%m-%d %H:%M")
    return templates.TemplateResponse(request, "experiment_detail.html", {
        "experiment": exp,
        "created_fmt": created_fmt,
    })


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/experiments")
async def api_list_experiments():
    rows, _ = db.list_experiments()
    return rows


@app.post("/api/experiments", status_code=201)
async def api_create_experiment(request: Request):
    body = await request.json()
    patient_number = (body.get("patientNumber") or body.get("name") or "").strip() or None
    distance = body.get("distanceMeters") or body.get("distance_meters")
    distance = float(distance) if distance not in (None, "") else None
    exp_id = db.create_experiment(
        patient_number=patient_number,
        height=body.get("height"),
        age=body.get("age"),
        weight=body.get("weight"),
        properties=body.get("properties") or {},
        distance_meters=distance,
        created_at=time.time(),
    )
    return {"id": exp_id}


@app.delete("/api/experiments/{exp_id}", status_code=204)
async def api_delete_experiment(exp_id: int):
    if not db.get_experiment(exp_id):
        raise HTTPException(status_code=404, detail="Not found")
    db.delete_experiment(exp_id)
    return Response(status_code=204)


@app.delete("/api/exercises/{ex_id}", status_code=204)
async def api_delete_exercise(ex_id: int):
    if not db.get_exercise(ex_id):
        raise HTTPException(status_code=404, detail="Not found")
    db.delete_exercise(ex_id)
    return Response(status_code=204)


@app.get("/api/experiments/{exp_id}")
async def api_get_experiment(exp_id: int):
    exp = db.get_experiment(exp_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Not found")
    exercises = db.list_exercises(exp_id)
    return {**exp, "exercises": exercises}


@app.post("/api/experiments/{exp_id}/start")
async def api_start_recording(exp_id: int):
    if _recording:
        raise HTTPException(status_code=409, detail="A recording is already in progress")
    exp = db.get_experiment(exp_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")
    ex_id = db.create_exercise(exp_id, properties={}, created_at=time.time())
    _start_rec(exercise_id=ex_id)
    if _pi_ws:
        await _pi_ws.send_text(json.dumps({"cmd": "start_wav"}))
    await _broadcast_json({"t": "rec_state", "recording": True, "exercise_id": ex_id})
    return {"exercise_id": ex_id}


@app.post("/api/exercises/{ex_id}/stop")
async def api_stop_recording(ex_id: int):
    global _exercise_id
    if not _recording:
        raise HTTPException(status_code=409, detail="No recording in progress")
    if _exercise_id != ex_id:
        raise HTTPException(status_code=409, detail="Exercise ID mismatch")
    _stop_rec()
    if _pi_ws:
        await _pi_ws.send_text(json.dumps({"cmd": "stop_wav"}))
    await _broadcast_json({"t": "rec_state", "recording": False})
    return {"ok": True}


@app.get("/api/exercises/{ex_id}/download")
async def api_download_exercise(ex_id: int):
    ex = db.get_exercise(ex_id)
    if not ex:
        raise HTTPException(status_code=404, detail="Exercise not found")

    blobs = db.get_sensor_blobs(ex_id)
    exp = db.get_experiment(ex["experiment_id"])
    distance_m = exp.get("distance_meters") if exp else None
    ts_str = datetime.fromtimestamp(ex["recording_started_at"] or ex["created_at"]).strftime("%Y%m%d_%H%M%S")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sensor, gz in blobs.items():
            raw = zlib.decompress(gz).decode()
            content = _audio_export_csv(raw, ex) if sensor == "audio" else raw
            zf.writestr(f"{sensor}_{ts_str}.csv", content)
        if ex.get("wav_filename"):
            wav_path = RECORDINGS_DIR / ex["wav_filename"]
            if wav_path.exists():
                zf.write(wav_path, arcname=ex["wav_filename"])
        feat_csv = _run_features(blobs, distance_m, f"exercise_{ex_id}")
        if feat_csv:
            zf.writestr("features.csv", feat_csv)
    buf.seek(0)

    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="exercise_{ex_id}_{ts_str}.zip"'},
    )


@app.get("/api/experiments/{exp_id}/download")
async def api_download_experiment(exp_id: int):
    exp = db.get_experiment(exp_id)
    if not exp:
        raise HTTPException(status_code=404, detail="Experiment not found")

    exercises = db.list_exercises(exp_id)
    distance_m = exp.get("distance_meters")
    all_feat_rows: list[str] = []

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for ex in exercises:
            if ex.get("recording_ended_at") is None:
                continue  # skip in-progress
            blobs = db.get_sensor_blobs(ex["id"])
            ts_str = datetime.fromtimestamp(ex["recording_started_at"] or ex["created_at"]).strftime("%Y%m%d_%H%M%S")
            prefix = f"exercise_{ex['id']}_{ts_str}"
            for sensor, gz in blobs.items():
                raw = zlib.decompress(gz).decode()
                content = _audio_export_csv(raw, ex) if sensor == "audio" else raw
                zf.writestr(f"{prefix}/{sensor}.csv", content)
            if ex.get("wav_filename"):
                wav_path = RECORDINGS_DIR / ex["wav_filename"]
                if wav_path.exists():
                    zf.write(wav_path, arcname=f"{prefix}/{ex['wav_filename']}")
            feat_csv = _run_features(blobs, distance_m, f"exercise_{ex['id']}")
            if feat_csv:
                zf.writestr(f"{prefix}/features.csv", feat_csv)
                all_feat_rows.append(feat_csv)

        # Combined feature table across all exercises
        if all_feat_rows:
            try:
                import pandas as pd
                frames = [pd.read_csv(io.StringIO(r)) for r in all_feat_rows]
                combined_csv = pd.concat(frames, ignore_index=True).to_csv(index=False)
                zf.writestr("features_all_exercises.csv", combined_csv)
            except Exception:
                pass
    buf.seek(0)

    safe_name = (exp.get("patient_number") or str(exp["id"])).replace(" ", "_")[:40]
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="experiment_{exp_id}_{safe_name}.zip"'},
    )


# ── Feature engineering ───────────────────────────────────────────────────────

def _run_features(blobs: dict[str, bytes], distance_m: Optional[float],
                  session_id: str) -> Optional[str]:
    """Compute features for one exercise. Returns CSV string or None on failure."""
    try:
        from feature_engineering import extract_features
        import pandas as pd

        imu_gz   = blobs.get("imu")
        audio_gz = blobs.get("audio")
        mar_gz   = blobs.get("mar")

        if not imu_gz:
            return None

        imu_csv   = zlib.decompress(imu_gz).decode()
        audio_csv = zlib.decompress(audio_gz).decode() if audio_gz else None
        mar_csv   = zlib.decompress(mar_gz).decode()   if mar_gz   else None

        files = {
            "imu":        io.StringIO(imu_csv),
            "audio":      io.StringIO(audio_csv) if audio_csv else None,
            "mouth":      io.StringIO(mar_csv)   if mar_csv   else None,
            "distance_m": distance_m,
        }

        features = extract_features(files)
        features["session"] = session_id

        df = pd.DataFrame([features])
        cols = ["session"] + [c for c in df.columns if c != "session"]
        out = io.StringIO()
        df[cols].to_csv(out, index=False)
        return out.getvalue()

    except Exception:
        return None


# ── Mock data generator ───────────────────────────────────────────────────────

async def _mock_loop() -> None:
    cv2 = None
    np = None
    try:
        import cv2 as _cv2
        import numpy as _np
        cv2, np = _cv2, _np
    except ImportError:
        pass

    period = 1.0 / 60
    t0 = time.time()
    tick = 0

    while True:
        t = time.time()
        elapsed = t - t0

        await _ingest({
            "type": "imu", "ts": t,
            "accel_x": int(1000 * math.sin(elapsed * 2.1)),
            "accel_y": int(600  * math.cos(elapsed * 1.7)),
            "accel_z": int(16384 + 300 * math.sin(elapsed * 0.8)),
            "gyro_x":  int(300 * math.sin(elapsed * 3.3)),
            "gyro_y":  int(200 * math.cos(elapsed * 2.5)),
            "gyro_z":  int(100 * math.sin(elapsed * 1.2)),
        })

        await _ingest({
            "type": "audio", "ts": t,
            "spl": max(0.0, 0.04 + 0.02 * math.sin(elapsed * 4) + random.gauss(0, 0.005)),
        })

        if tick % 4 == 0:
            if cv2 is not None and np is not None:
                r = int(127 + 127 * math.sin(elapsed * 0.5))
                g = int(127 + 127 * math.sin(elapsed * 0.7))
                b = int(127 + 127 * math.sin(elapsed * 0.3))
                img = np.full((240, 320, 3), (b, g, r), dtype=np.uint8)
                cv2.putText(img, f"MOCK  t={elapsed:.1f}s", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                _, frame_buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 50])
                jpeg = bytes(frame_buf)
                jpeg_b64 = base64.b64encode(jpeg).decode()
                await _ingest({"type": "camera", "ts": t, "jpeg": jpeg_b64})
            else:
                vertical   = max(0.0, 0.05 + 0.04 * math.sin(elapsed * 1.8))
                horizontal = max(0.01, 0.25 + 0.02 * math.sin(elapsed * 0.5))
                await _ingest({"type": "mar", "ts": t, "vertical": vertical, "horizontal": horizontal})

        tick += 1
        await asyncio.sleep(period)


@app.on_event("startup")
async def _startup():
    global _mar_queue
    db.init_db()
    _mar_queue = asyncio.Queue(maxsize=1)
    asyncio.create_task(_mar_worker())
    if MOCK:
        asyncio.create_task(_mock_loop())


# ── Spec-compliant API router (must be before catch-all static mount) ──────────

from app_v2.api_router import router as _spec_router  # noqa: E402
app.include_router(_spec_router)

# ── Static files (catch-all — must be last) ───────────────────────────────────

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
