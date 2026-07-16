# Tasks: Pi Sensor Streaming & Research Dashboard

All tasks listed here are **implemented and complete**. This document reflects the as-built state.

---

## Pi access (for deployment / re-running)

- SSH: `pi007@192.168.0.133`, password `raspberry007`
- Project root on Pi: `/home/pi007/.virtualenvs/iot-project/`
- Python: `/home/pi007/.virtualenvs/iot-project/.venv/bin/python`
- After any Pi-side file change: `scp <file> pi007@192.168.0.133:/home/pi007/.virtualenvs/iot-project/<path>`

---

## Task 1 — `config.py`: shared configuration ✅

Constants for network, sensor rates, camera, scaling, and buffer size. All values overridable via environment variables (`PI_SERVER_HOST`, `PI_INGEST_PORT`, `PI_CAMERA_HZ`, `PI_CAMERA_WIDTH`, `PI_CAMERA_HEIGHT`, `PI_CAMERA_QUALITY`). Rates updated to 60 Hz (was 20 Hz).

---

## Task 2 — `server/buffer.py`: thread-safe circular buffer ✅

`SensorBuffer` with deques per sensor type (IMU, audio, NHM/MAR) plus latest-frame-only camera. `push_nhm` stores `{"ts": float, "nhm": float|None}`. `get_*(since=ts)` returns only frames newer than cursor.

---

## Task 3 — `server/cv_processor.py`: MAR computation ✅

**Changed from original design:**
- Uses MediaPipe **FaceLandmarker Tasks API** (not deprecated `mp.solutions.face_mesh`)
- Computes **MAR** = `(vertical, horizontal)` tuple using normalized landmark coordinates, NOT interocular distance
  - `vertical = |lm[13].y − lm[14].y|`
  - `horizontal = |lm[61].x − lm[291].x|`
- Detection confidence thresholds lowered to 0.3 for reliable detection at 320×240 JPEG q50
- Returns `tuple[float, float] | None` (not `float | None`)
- Function name kept as `compute_nhm` for backward compatibility

---

## Task 4 — `app_v2/server.py`: single FastAPI process ✅

**Replaces both the old `server/ingest.py` and Streamlit.** Key additions beyond the original design:

- **Camera color correction**: BGR→RGB channel swap + optional rotation (`CAMERA_ROTATE` env var) applied server-side before browser broadcast
- **Single MAR worker** (`_mar_worker`): one `asyncio.Task` consuming `asyncio.Queue(maxsize=1)`; old frames overwritten by newer ones, preventing latency backlog
- **Recording state** tied to exercises in DB (`_exercise_id`, `_latest_exercise_id`)
- **WAV association**: `POST /wav` from Pi updates `exercises.wav_filename` via `_latest_exercise_id`
- **Mock mode**: `MOCK=1` generates synthetic sensor data at 60 Hz without Pi
- **Debug endpoint**: `GET /debug/camera` returns latest camera frame for diagnostics

CSV column headers:
- `mar`: `["timestamp", "vertical", "horizontal"]` (two components, not single ratio)

---

## Task 5 — `app_v2/db.py`: SQLite persistence ✅

**New — not in original design.** Three tables with WAL mode:
- `experiments`: patient metadata + optional `distance_meters`
- `exercises`: recording state machine (`idle` → `recording` → `stopped`), timestamps, wav_filename
- `sensor_blobs`: zlib-compressed CSV per sensor per exercise

Migrations in `init_db()` handle schema evolution without data loss. Sensor type `nhm` migrated to `mar`.

---

## Task 6 — `pi/streamer.py`: Pi-side WebSocket client ✅

**Key change from design:** Uses `SharedAudioProvider` (single ALSA stream) instead of separate `RealAudioProvider` + `sounddevice.InputStream` for WAV, avoiding ALSA exclusive-lock errors.

Imports now use `from pi.sensors import RealIMUProvider, RealCameraProvider` after `sensors.py` was moved into `pi/`.

---

## Task 7 — live dashboard (`app_v2/static/`) ✅

**Replaces Streamlit pages.** Built with uPlot + vanilla JS:
- WebSocket push from server (not HTTP polling)
- `requestAnimationFrame` render loop — data at 60 Hz, display at screen refresh rate
- Binary `CAM:` prefix frames for camera
- MAR chart uses `msg.vertical / msg.horizontal` ratio
- Reconnects automatically on disconnect

---

## Task 8 — experiment management dashboard ✅

**New — not in original design.** Jinja2-rendered pages at `/dashboard`:
- `experiments.html`: paginated list with patient metadata columns
- `experiment_new.html`: form with `patientNumber`, `height`, `age`, `weight`, optional `distanceMeters`
- `experiment_detail.html`: exercise table with recording state, duration, sensor badges, download

Recording flow (two-step, matching REST API spec):
1. `POST /experiments/{id}/exercises` → creates exercise with `recordingStatus: idle`
2. `POST /exercises/{id}/recording/start` → starts accumulation
3. `POST /exercises/{id}/recording/stop` → compresses blobs → writes to DB

---

## Task 9 — `app_v2/api_router.py`: spec-compliant REST API ✅

**New — not in original design.** Implements `openapi.yaml` exactly:
- String IDs (stringified integers), ISO 8601 timestamps
- Paginated `GET /experiments` and `GET /exercises`
- `PATCH /experiments/{id}` for partial updates
- `DELETE /exercises/{id}/data` clears blobs, resets `recordingStatus` to `idle`
- `GET /exercises/{id}/data` computes and returns `mouthOpening`, `soundPressure`, `footSpeed`, `aggregates`

**`footSpeed` computation:**
- Detects steps in IMU `accel_z(g)` via `feature_engineering.detect_steps()`
- Auto-corrects inverted Z axis (mean < 0 → negate)
- Step length: `distanceMeters * 100 / step_count` cm, or `0.2075 * height_cm` fallback
- Returns per-step foot speeds in cm/s

---

## Task 10 — `feature_engineering.py`: offline feature extraction ✅

Updated `compute_video_features` to handle both new (`vertical`, `horizontal` columns) and legacy (`mar` column) CSV formats.

Step detection threshold (`STEP_HEIGHT_G = 1.2`) applies to corrected (non-inverted) accel_z.

---

## Task 11 — file structure cleanup ✅

- Deleted: `app.py`, `pages/`, `data.py`, `recording.wav`, `requirements.txt`, `server/ingest.py`, `server/api_client.py`
- Created: `diagnostics/` with `camera.py`, `gyro.py`, `microphone.py`
- Moved: `sensors.py` → `pi/sensors.py`, `requirements_pi.txt` → `pi/`, `requirements_server.txt` → `app_v2/`
- Added: `app_v2/WEBSOCKET.md` (WebSocket protocol documentation)
- Added: `backups/` (DB snapshots + experiment ZIPs)

---

## Running the system

### Server
```bash
# From project root
CAMERA_ROTATE=90CW uvicorn app_v2.server:app --host 0.0.0.0 --port 8080

# Mock mode (no Pi needed)
MOCK=1 uvicorn app_v2.server:app --host 0.0.0.0 --port 8080
```

### Pi
```bash
PI_SERVER_HOST=192.168.0.182 PI_INGEST_PORT=8080 \
  /home/pi007/.virtualenvs/iot-project/.venv/bin/python \
  /home/pi007/.virtualenvs/iot-project/pi/streamer.py
```

---

## Smoke test checklist

- [x] Pi streamer connects → dashboard shows "Pi connected"
- [x] IMU, audio, camera, MAR charts all update live
- [x] MAR latency ≲ 300 ms above camera frame display
- [x] Disconnect Pi → "Waiting for Pi…" within 5 s
- [x] Create experiment with patient metadata via `/dashboard`
- [x] Start/stop recording → exercise appears in detail page with sensor badges
- [x] Exercise download ZIP contains `imu_*.csv`, `audio_*.csv`, `mar_*.csv`, `features.csv`
- [x] `GET /exercises/{id}/data` returns non-null `footSpeed` and `stepLengths` when height/distance set
- [x] `DELETE /exercises/{id}/data` resets to `recordingStatus: idle`; re-recording allowed
- [x] `CAMERA_ROTATE=90CW` corrects sideways camera for both browser display and MAR detection
- [x] Inverted IMU (Z pointing down) auto-corrected in step detection
