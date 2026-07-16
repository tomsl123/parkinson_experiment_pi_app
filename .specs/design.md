# Design: Pi Sensor Streaming & Research Dashboard

## Architecture Overview

```
┌───────────────────────────────┐        ┌─────────────────────────────────────────────────┐
│  Raspberry Pi                 │        │  Server (app_v2/server.py — single FastAPI proc) │
│                               │        │                                                  │
│  pi/streamer.py               │  WS    │  WS /stream   ← Pi connects here                │
│    IMU, audio, camera ────────┼───────►│  WS /ws       → browser clients                 │
│    50 Hz / 50 Hz / 30 Hz      │        │                                                  │
│    320×240 JPEG q50           │        │  REST /experiments, /exercises  (openapi.yaml)   │
│    Reconnects on drop         │        │  REST /api/experiments, /api/exercises  (dash)   │
│    Receives control ◄─────────┼────────│  POST /wav    ← Pi uploads WAV                  │
│    Records WAV locally        │        │                                                  │
│    Uploads WAV ───────────────┼───────►│  server/buffer.py   (thread-safe rolling buffer) │
│                               │        │  server/cv_processor.py  (MAR via MediaPipe)     │
└───────────────────────────────┘        │  app_v2/db.py  (SQLite experiment storage)       │
                                         │  feature_engineering.py  (step detection, stats) │
                                         └────────────────────┬────────────────────────────┘
                                                              │ HTTP / WS
                                              ┌───────────────┴───────────────┐
                                              │  Browser                      │
                                              │  /          live dashboard    │
                                              │  /dashboard experiment mgmt   │
                                              └───────────────────────────────┘
```

**One process** on the server handles everything: Pi WebSocket ingestion, browser WebSocket push, REST API, template serving, and the MAR worker. There is no separate Streamlit process or ingest sidecar.

---

## Directory Structure

```
pi-stuff/
├── config.py                    # shared constants: host, port, rates, scales
├── feature_engineering.py       # step detection, gait/voice/mouth features
├── face_landmarker.task         # MediaPipe FaceLandmarker model bundle
│
├── pi/
│   ├── streamer.py              # WebSocket client; runs on Pi
│   ├── sensors.py               # RealIMUProvider, RealCameraProvider (Pi hardware)
│   └── requirements_pi.txt
│
├── app_v2/
│   ├── server.py                # FastAPI: Pi WS, browser WS, REST, static serving
│   ├── api_router.py            # Spec-compliant REST router (openapi.yaml)
│   ├── db.py                    # SQLite layer: experiments, exercises, sensor blobs
│   ├── __init__.py
│   ├── openapi.yaml             # REST API specification
│   ├── WEBSOCKET.md             # WebSocket protocol documentation
│   ├── experiments.db           # SQLite database (runtime, not committed)
│   ├── requirements_server.txt
│   ├── static/
│   │   ├── index.html           # live sensor dashboard (uPlot charts)
│   │   ├── app.js               # WebSocket client + chart rendering
│   │   └── dashboard.js         # shared JS for experiment pages
│   └── templates/               # Jinja2 templates for experiment management
│       ├── experiments.html
│       ├── experiment_new.html
│       └── experiment_detail.html
│
├── server/
│   ├── buffer.py                # SensorBuffer: thread-safe deques per sensor
│   └── cv_processor.py          # compute_nhm(): MAR via MediaPipe FaceLandmarker
│
├── diagnostics/                 # standalone peripheral test scripts (Pi-only)
│   ├── camera.py
│   ├── gyro.py
│   └── microphone.py
│
└── backups/                     # DB and experiment ZIP backups
```

---

## Component Designs

### 1. `config.py` — shared configuration

```python
SERVER_HOST = os.getenv("PI_SERVER_HOST", "127.0.0.1")
INGEST_PORT = int(os.getenv("PI_INGEST_PORT", "8080"))

IMU_HZ    = 50
AUDIO_HZ  = 50
CAMERA_HZ = int(os.getenv("PI_CAMERA_HZ", "30"))

CAMERA_WIDTH        = int(os.getenv("PI_CAMERA_WIDTH",   "320"))
CAMERA_HEIGHT       = int(os.getenv("PI_CAMERA_HEIGHT",  "240"))
CAMERA_JPEG_QUALITY = int(os.getenv("PI_CAMERA_QUALITY", "50"))

WINDOW_SECONDS = 10
ACCEL_SCALE    = 16384.0   # LSB/g    (MPU6050 ±2g)
GYRO_SCALE     = 131.0     # LSB/°/s  (MPU6050 ±250°/s)
```

Server also reads:
- `CAMERA_ROTATE` — `90CW` | `90CCW` | `180` — rotates every camera frame before display and MAR inference (corrects physically sideways camera mounting).
- `MOCK=1` — generates synthetic sensor data without a Pi.

---

### 2. `pi/streamer.py` — Pi-side WebSocket client

Uses a **single shared audio stream** (`SharedAudioProvider`) for both SPL streaming and WAV recording, avoiding the ALSA exclusive-lock problem when WAV recording starts mid-session.

**Startup sequence:**
1. Check NTP sync; log warning if not synced.
2. Try each sensor independently (IMU, audio, camera); log and skip on failure.
3. Enter reconnect loop: connect → `asyncio.gather(imu_loop, audio_loop, camera_loop, control_loop)` → on disconnect, backoff and retry.

**Frame formats sent to server:**

```json
{"type": "imu",    "ts": 1750000000.123, "accel_x": 500, "accel_y": -200, "accel_z": -16200, "gyro_x": 100, "gyro_y": -50, "gyro_z": 30}
{"type": "audio",  "ts": 1750000000.124, "spl": 0.045}
{"type": "camera", "ts": 1750000000.130, "jpeg": "<base64 JPEG>"}
```

**Incoming control messages (server → Pi):**
```json
{"cmd": "start_wav"}
{"cmd": "stop_wav"}
```

**WAV recording:** On `start_wav`, `SharedAudioProvider.start_wav_capture()` attaches a queue to the existing audio callback. The `_record_wav_task` drains this queue into a soundfile, then HTTP POSTs the result to `/wav` on stop.

**Camera encoding:** The Pi captures RGB from picamera2, converts to BGR, resizes to `CAMERA_WIDTH × CAMERA_HEIGHT`, and JPEG-encodes. The resulting JPEG has R/B swapped relative to standard RGB JPEG — the server corrects this before forwarding to browsers.

---

### 3. `server/cv_processor.py` — MAR computation

Uses the **MediaPipe FaceLandmarker Tasks API** (not the deprecated `mp.solutions` API).

```python
def compute_nhm(jpeg_bytes: bytes) -> tuple[float, float] | None:
    """Return (vertical, horizontal) mouth opening normalised to frame size, or None."""
    # vertical   = |lm[13].y - lm[14].y|   (upper lip - lower lip, normalised)
    # horizontal = |lm[61].x - lm[291].x|  (left corner - right corner, normalised)
```

Returns a `(vertical, horizontal)` tuple — not a ratio — so each component is available independently for the spec API. MAR ratio = `vertical / horizontal`.

Detection confidence thresholds are set to **0.3** (default 0.5 is too strict for 320×240 JPEG at quality 50).

The function is **not thread-safe** — calls from multiple threads would corrupt the model state. A single `_mar_worker` asyncio task serialises all calls via an `asyncio.Queue(maxsize=1)`.

---

### 4. `server/buffer.py` — in-memory circular buffer

Thread-safe store shared between the FastAPI WebSocket handler (writer) and the REST/WS broadcast path (readers). One `threading.Lock` protects all mutations.

- `push_nhm(ts, nhm)` stores `{"ts": float, "nhm": float|None}` — `nhm` is None when no face detected.
- Camera: stores only the **latest frame** (not a history), since video is stateless for live display.
- `maxlen = WINDOW_SECONDS * max(IMU_HZ, AUDIO_HZ)` — large enough for any burst.

---

### 5. `app_v2/server.py` — single FastAPI process

**WebSocket endpoints:**

| Path | Direction | Purpose |
|---|---|---|
| `/stream` | Pi → Server → (side-effect) Browser | Sensor data ingestion |
| `/ws` | Server → Browser | Live push of processed sensor data |

**Camera ingest pipeline (per frame):**
1. Base64-decode the JPEG.
2. Decode with cv2, apply optional rotation (`CAMERA_ROTATE`), correct R/B swap (BGR → RGB), re-encode at quality 50 → `jpeg_display`.
3. Store raw JPEG in `_buffer` (for future REST queries).
4. Broadcast `b"CAM:" + jpeg_display` to all `/ws` clients.
5. Place raw JPEG into `_mar_queue` (drops stale frame if queue full).

**MAR worker (`_mar_worker`):**
- Single `asyncio.Task` started at server startup.
- Awaits `asyncio.Queue(maxsize=1)` — new frames overwrite unprocessed ones.
- Calls `compute_nhm` in a thread executor (~150–250 ms).
- Broadcasts `{"t": "mar", "ts": ..., "vertical": ..., "horizontal": ...}` to `/ws` clients.
- Effective MAR rate: ~5–8 Hz (bounded by inference time, not camera rate).

**Recording state:**
```python
_recording: bool          # True while data is accumulating
_rec_rows: dict[str, list]  # {"imu": [...], "audio": [...], "mar": [...]}
_exercise_id: int | None  # active exercise in DB
_latest_exercise_id: int | None  # for WAV association after stop
_mar_queue: asyncio.Queue  # single-slot queue for MAR worker
```

**CSV column headers (stored as zlib-compressed blobs in DB):**
```python
"imu":   ["timestamp", "accel_x(g)", "accel_y(g)", "accel_z(g)", "gyro_x(dps)", "gyro_y(dps)", "gyro_z(dps)"]
"audio": ["timestamp", "spl"]
"mar":   ["timestamp", "vertical", "horizontal"]
```

---

### 6. `app_v2/db.py` — SQLite persistence

WAL mode enabled. Three tables:

```sql
experiments (id, patient_number, height, age, weight, properties JSON,
             distance_meters, created_at)

exercises   (id, experiment_id, created_at, recording_status,
             recording_started_at, recording_ended_at, wav_filename, notes, properties JSON)

sensor_blobs (id, exercise_id, sensor_type TEXT, csv_gz BLOB)
-- sensor_type in ('imu', 'audio', 'mar')
-- csv_gz: zlib-compressed CSV bytes including header row
```

Sensor data is stored as **per-exercise compressed blobs** rather than individual rows. At 50 Hz a 60-second exercise produces ~3,000 rows per sensor; as compressed CSV this is ~40–80 KB per blob — far smaller and faster than row-per-sample.

Migrations run in `init_db()` via `ALTER TABLE … ADD COLUMN` wrapped in try/except, making the schema forward-compatible with existing DBs.

---

### 7. `app_v2/api_router.py` — spec-compliant REST API

Implements all endpoints defined in `openapi.yaml`. Mounted at `/` (no prefix) alongside the dashboard routes.

**`GET /exercises/{id}/data` — computed data response:**

1. Load sensor blobs from DB, decompress.
2. `mouthOpening`: parse MAR CSV → list of `[vertical, horizontal]` tuples.
3. `soundPressure`: parse audio CSV → list of SPL floats.
4. `footSpeed`: detect steps in IMU data via `feature_engineering.detect_steps()`.
   - Auto-corrects inverted Z axis: if `mean(accel_z) < 0`, negate the column.
   - Step length: `distance_m * 100 / step_count` cm if distance set; else `0.2075 * height_cm`.
   - Per-step foot speed: `step_length_cm / step_interval_s`.
5. `aggregates`: numpy mean/median of each signal.

IDs are stringified integers. Timestamps are ISO 8601 UTC strings.

---

### 8. `feature_engineering.py` — offline feature extraction

Used both by `api_router.py` (for the data endpoint) and optionally standalone for batch CSV analysis.

Key function: `detect_steps(df_imu)` — finds peaks in `accel_z(g)` above `STEP_HEIGHT_G = 1.2`. Caller must correct inverted Z before calling.

`compute_video_features(df_mouth)` accepts both new (`vertical`, `horizontal` columns) and legacy (`mar` column) CSV formats.

---

## Data Flow: Camera → MAR

```
Pi camera (30 Hz BGR JPEG)
  → base64 → JSON {"type": "camera", ...}
  → WebSocket /stream
Server _ingest():
  → decode JPEG → rotate (if CAMERA_ROTATE set) → fix BGR→RGB → re-encode
  → broadcast b"CAM:" + jpeg_display  (to browser, ~instant)
  → _mar_queue.put_nowait((raw_jpeg, ts))  (overwrites stale frame)
_mar_worker (single task):
  → await queue.get()
  → run_in_executor: compute_nhm(jpeg)  (~150-250 ms)
  → broadcast {"t": "mar", "ts": ..., "vertical": ..., "horizontal": ...}
Browser app.js:
  → msg.vertical / msg.horizontal → bufPush(bufs.nhm, ...) → uPlot.setData()
```

---

## Deployment

### Server

```bash
# From project root
CAMERA_ROTATE=90CW uvicorn app_v2.server:app --host 0.0.0.0 --port 8080

# With mock data (no Pi needed)
MOCK=1 uvicorn app_v2.server:app --host 0.0.0.0 --port 8080
```

Dashboard: `http://localhost:8080`  
Experiment management: `http://localhost:8080/dashboard`

### Pi

```bash
PI_SERVER_HOST=192.168.0.182 PI_INGEST_PORT=8080 \
  /home/pi007/.virtualenvs/iot-project/.venv/bin/python \
  /home/pi007/.virtualenvs/iot-project/pi/streamer.py
```

After syncing files to Pi:
```bash
scp pi/streamer.py pi007@192.168.0.133:/home/pi007/.virtualenvs/iot-project/pi/
scp pi/sensors.py  pi007@192.168.0.133:/home/pi007/.virtualenvs/iot-project/pi/
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Single FastAPI process | Eliminates Streamlit's polling overhead and the two-process sidecar; WebSocket push is orders of magnitude lower latency than 4 Hz HTTP polling |
| uPlot for charts | 40 KB, purpose-built for high-frequency time series; 10–100× faster than Plotly |
| MAR = vertical/horizontal lip (not interocular) | Works when only the mouth region is visible; scale-invariant; matches the spec API |
| MediaPipe Tasks API (FaceLandmarker) | Replaces deprecated `mp.solutions` API; confidence threshold 0.3 needed for 320×240 JPEG q50 |
| Single MAR worker + Queue(maxsize=1) | Bounds MAR latency to one inference cycle; eliminates the backlog that grows when inference time > frame interval |
| Zlib-compressed CSV blobs in DB | Write-once/read-all access pattern; blob per exercise is 50–100 KB vs potentially millions of individual rows |
| distanceMeters as optional experiment field | Spec-conformant when omitted; enables accurate foot speed when provided |
| Z-axis auto-correction | IMU mounted upside-down (common) makes accel_z mean negative; auto-flip avoids misconfiguration silently breaking step detection |
| Camera BGR→RGB server-side fix | Pi's picamera2 returns BGR; correcting on server avoids Pi code change and keeps the fix in one place |
| WAV associated with exercise via _latest_exercise_id | WAV upload arrives asynchronously ~1–2 s after stop; global variable bridges the async gap without blocking |
