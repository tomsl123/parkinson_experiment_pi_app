# Pi Sensor Streaming & Research Dashboard

A system for streaming IMU, audio, and camera data from a Raspberry Pi to a server,
with real-time visualization, experiment management, and a REST API for external clients.

## Overview

The Pi runs a lightweight WebSocket streamer. The server receives sensor data,
computes Mouth Aspect Ratio (MAR) via MediaPipe, stores recordings in SQLite,
and serves a browser dashboard and a REST API.

```
Raspberry Pi                         Server
──────────────                       ──────────────────────────────────
pi/streamer.py ──── WS /stream ────► app_v2/server.py
  IMU   50 Hz                          ├── live dashboard  /
  Audio 50 Hz                          ├── experiment mgmt /dashboard
  Camera 30 Hz                         ├── REST API        /experiments  /exercises
  WAV recording                        └── SQLite DB       app_v2/experiments.db
```

## Repository Structure

```
pi-stuff/
├── config.py                    # shared constants (rates, scales, network)
├── feature_engineering.py       # step detection, gait/voice/mouth features
├── face_landmarker.task         # MediaPipe FaceLandmarker model
│
├── pi/
│   ├── streamer.py              # Pi WebSocket client (run this on the Pi)
│   ├── sensors.py               # hardware providers (IMU, camera)
│   └── requirements_pi.txt
│
├── app_v2/
│   ├── server.py                # FastAPI: WebSocket ingest + browser push + REST
│   ├── api_router.py            # spec-compliant REST API (openapi.yaml)
│   ├── db.py                    # SQLite layer
│   ├── openapi.yaml             # REST API specification
│   ├── WEBSOCKET.md             # WebSocket protocol documentation
│   ├── requirements_server.txt
│   ├── static/                  # live dashboard (uPlot + vanilla JS)
│   └── templates/               # experiment management pages (Jinja2)
│
├── server/
│   ├── buffer.py                # thread-safe rolling sensor buffer
│   └── cv_processor.py          # MAR computation via MediaPipe
│
├── diagnostics/                 # standalone peripheral test scripts (Pi only)
│   ├── camera.py
│   ├── gyro.py
│   └── microphone.py
│
└── backups/                     # DB and experiment ZIP backups
```

## Setup

### Server (Mac / Linux)

```bash
pip install -r app_v2/requirements_server.txt

# Run (with camera rotation correction if camera is mounted sideways)
CAMERA_ROTATE=90CW uvicorn app_v2.server:app --host 0.0.0.0 --port 8080

# Run without Pi (mock data)
MOCK=1 uvicorn app_v2.server:app --host 0.0.0.0 --port 8080
```

Open **http://localhost:8080** for the live dashboard,
**http://localhost:8080/dashboard** for experiment management.

### Pi

```bash
# Install deps (on the Pi)
pip install -r pi/requirements_pi.txt

# Start streaming
PI_SERVER_HOST=<server-ip> PI_INGEST_PORT=8080 python pi/streamer.py
```

After changing files locally, sync to Pi:
```bash
scp pi/streamer.py pi/sensors.py pi007@192.168.0.133:/home/pi007/.virtualenvs/iot-project/pi/
scp config.py pi007@192.168.0.133:/home/pi007/.virtualenvs/iot-project/
```

Pi SSH: `pi007@192.168.0.133`, password `raspberry007`  
Pi project root: `/home/pi007/.virtualenvs/iot-project/`

## Configuration

All values in `config.py` are overridable via environment variables on the server or Pi.

| Variable | Default | Description |
|---|---|---|
| `PI_SERVER_HOST` | `127.0.0.1` | Server IP (set on Pi) |
| `PI_INGEST_PORT` | `8080` | Server port (set on Pi) |
| `PI_CAMERA_HZ` | `30` | Camera frame rate |
| `PI_CAMERA_WIDTH` | `320` | Camera width in pixels |
| `PI_CAMERA_HEIGHT` | `240` | Camera height in pixels |
| `PI_CAMERA_QUALITY` | `50` | JPEG quality (1–95) |
| `CAMERA_ROTATE` | _(none)_ | Rotate camera frames: `90CW`, `90CCW`, `180` |
| `MOCK` | `0` | Set to `1` to generate synthetic data without a Pi |

## Data Rates

| Sensor | Rate | Notes |
|---|---|---|
| IMU (accel + gyro) | 50 Hz | Raw MPU6050 integer counts |
| Audio SPL | 50 Hz | RMS amplitude (float) |
| Camera | 30 Hz | 320×240 JPEG, quality 50 |
| MAR | ~5–8 Hz | Computed server-side via MediaPipe |

## REST API

The server exposes a spec-compliant REST API documented in `app_v2/openapi.yaml`.

Key endpoints:

| Method | Path | Description |
|---|---|---|
| `POST` | `/experiments` | Create experiment with patient metadata |
| `GET` | `/experiments` | List experiments (paginated) |
| `POST` | `/experiments/{id}/exercises` | Create exercise within an experiment |
| `POST` | `/exercises/{id}/recording/start` | Start recording |
| `POST` | `/exercises/{id}/recording/stop` | Stop recording and save to DB |
| `GET` | `/exercises/{id}/data` | Get processed sensor data (MAR, SPL, foot speed, aggregates) |
| `DELETE` | `/exercises/{id}/data` | Clear data and allow re-recording |

See `app_v2/WEBSOCKET.md` for the WebSocket protocol used by the Pi and browser clients.

## Sensors

The Pi reads:
- **IMU**: MPU6050 via I2C (`smbus2`). Scales: 16384 LSB/g, 131 LSB/°/s.
- **Audio**: single sounddevice ALSA stream (48 kHz, mono) shared between SPL streaming and WAV recording.
- **Camera**: picamera2 (OV5647), RGB output converted to JPEG server-side after BGR correction.

If a sensor is unavailable at startup, the streamer skips it and continues with the rest.

## Experiment Management

Experiments represent a patient session. Each experiment has:
- Patient number, height (cm), age, weight (kg)
- Optional walk distance in metres (improves foot speed accuracy)

Each experiment contains one or more **exercises** (individual recording runs).
Sensor data is stored as zlib-compressed CSV blobs in SQLite and can be exported
as ZIP archives via the dashboard or REST API.

## Feature Engineering

`feature_engineering.py` computes gait and speech features from raw CSVs:

- **Step detection** from accel Z (auto-corrects inverted sensor mounting)
- **Cadence**, gait regularity, activity ratio
- **Walking speed** and step length (from distance or height estimate)
- **Loudness** mean, variability, trend, vocal activity ratio
- **MAR** mean, opening rate, variability, trend
- **Ba vocalization ratio** (speech peak per step)

The same module is used by the REST API data endpoint and can be run standalone
on exported CSVs.

## Specs

Detailed requirements, architecture design, and implementation tasks are in `.specs/`:
- `.specs/requirements.md`
- `.specs/design.md`
- `.specs/tasks.md`
