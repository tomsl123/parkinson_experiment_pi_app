# Requirements: Pi Sensor Streaming & Research Dashboard

## Overview

A Raspberry Pi streams raw sensor data (IMU, audio, camera) over WebSocket to a server.
The server processes the data, runs face landmark detection, and exposes it via a REST API
and a real-time browser dashboard. A relational experiment management layer organises
recordings into named experiments and exercises for later analysis.

---

## User Stories

### Streaming (Pi side)

**US-1** As an operator, I want the Pi to stream all sensor data to the server over WebSocket so that the Pi does no processing and stays lightweight.

**US-2** As an operator, I want the Pi to automatically reconnect with exponential backoff if the WebSocket connection drops, so that a temporary network hiccup requires no manual intervention.

**US-3** As an operator, I want the Pi to stream camera frames as compressed JPEG over the same connection, so that live video and mouth-opening data are available on the server.

### Live dashboard

**US-4** As a user, I want to open a browser and see live IMU (accelerometer + gyroscope), audio SPL, camera feed, and Mouth Aspect Ratio (MAR) charts updating in real time, so that I can monitor the Pi remotely.

**US-5** As a user, I want the dashboard to show a "Waiting for Pi…" state when no data is arriving, so that a disconnected Pi does not display stale or broken charts.

### Experiment management

**US-10** As a researcher, I want to create named experiments with patient metadata (patient number, height, age, weight, optional walk distance), so that recordings are organised and identifiable.

**US-11** As a researcher, I want to start and stop individual recording runs (exercises) within an experiment, so that I can capture multiple trials per session.

**US-12** As a researcher, I want all sensor data for each exercise stored persistently in a database, so that data survives server restarts and can be retrieved later.

**US-13** As a researcher, I want to download per-exercise and per-experiment ZIP archives containing CSV files and computed features, so that I can do offline analysis.

### REST API

**US-14** As a client application, I want a spec-compliant REST API (see `openapi.yaml`) for managing experiments and exercises and retrieving structured sensor data, so that external tools can integrate without a browser.

### Operations

**US-7** As an operator, I want the server to start with a single `uvicorn` command and the Pi with a single `python` command and two environment variables, so that setup is straightforward.

---

## Acceptance Criteria

### Pi streamer

**AC-1** WHEN the Pi starts the streamer THEN it SHALL establish a WebSocket connection to `ws://<server>:<port>/stream` within 5 seconds.

**AC-2** WHEN the connection is established THEN the Pi SHALL send sensor frames at 50 Hz for IMU and audio, and 30 Hz for camera (configurable via `PI_CAMERA_HZ`).

**AC-3** WHEN each frame is sent THEN it SHALL be a JSON message containing:
- `type`: one of `"imu"`, `"audio"`, `"camera"`
- `ts`: Unix timestamp (float) recorded **on the Pi at the moment of sensor read**
- For `imu`: `accel_x/y/z`, `gyro_x/y/z` (raw MPU6050 integer counts)
- For `audio`: `spl` (float RMS amplitude)
- For `camera`: `jpeg` (base64-encoded JPEG, 320×240 at JPEG quality 50)

**AC-3b** WHEN the Pi starts THEN it SHALL check NTP sync via `timedatectl` and log a warning if not synced.

**AC-4** WHEN the connection drops THEN the Pi SHALL retry with exponential backoff (2⁰, 2¹ … up to 30 s) and SHALL NOT crash.

**AC-5** WHEN a sensor is unavailable THEN the streamer SHALL skip that sensor type and continue streaming the rest.

### WAV recording

**AC-14** WHEN the server receives a `start_wav` control command from a client THEN it SHALL forward it to the Pi, and the Pi SHALL begin writing a local WAV file named `audio_<pi_ts>.wav`.

**AC-15** WHEN the server receives a `stop_wav` command THEN it SHALL forward it to the Pi, and the Pi SHALL finalize and HTTP POST the WAV to `POST /wav` on the server.

**AC-16** WHEN a WAV is received THEN it SHALL be stored in `recordings/` and associated with the most-recently completed exercise in the database.

### Server — sensor ingestion

**AC-6** WHEN the server starts THEN it SHALL accept WebSocket connections from a Pi at `/stream`. Only one Pi may be connected at a time; additional connections are rejected with close code 4000.

**AC-7** WHEN a frame arrives THEN the server SHALL store it in a thread-safe in-memory circular buffer covering the last 10 seconds per sensor type.

**AC-7a** WHEN a camera frame arrives THEN the server SHALL run MediaPipe FaceLandmarker (Tasks API) on it and compute MAR = |y_upper_lip − y_lower_lip| / |x_left_corner − x_right_corner| using normalized landmark coordinates. WHEN no face is detected THEN MAR is null.

**AC-8** WHEN a second Pi tries to connect THEN the server SHALL reject it immediately.

### Live dashboard

**AC-9** WHEN the dashboard loads THEN it SHALL display via WebSocket push: camera feed, rolling MAR chart, rolling SPL chart, rolling accelerometer chart, rolling gyroscope chart.

**AC-10** WHEN no data has arrived in the last 5 seconds THEN the dashboard SHALL show a "Pi connected" / "Waiting for Pi…" status indicator.

### Experiment management

**AC-20** WHEN a researcher creates an experiment THEN it SHALL be stored with `patientNumber`, `height`, `age`, `weight`, `distanceMeters` (optional), and a server-assigned `id` and `createdAt`.

**AC-21** WHEN a researcher creates an exercise and starts recording THEN the server SHALL begin accumulating sensor data into `_rec_rows` in memory. WHEN recording stops THEN all data SHALL be written to the database as zlib-compressed CSV blobs.

**AC-22** WHEN footSpeed is requested via the data API THEN it SHALL be computed from step detection on the IMU data. Step length SHALL use `distanceMeters` if set, otherwise estimated from patient height (0.2075 × height_cm per step).

**AC-23** WHEN an IMU sensor is mounted with Z pointing down (mean accel_z < 0) THEN the server SHALL auto-correct the Z axis before step detection.

**AC-24** WHEN a client calls `DELETE /exercises/{id}/data` THEN the sensor blobs SHALL be deleted and the exercise `recordingStatus` SHALL revert to `idle`, allowing re-recording.

### Configuration

**AC-13** WHEN deploying, the server address and port SHALL be configurable via `PI_SERVER_HOST` and `PI_INGEST_PORT` environment variables on the Pi. Camera rate, resolution, and rotation correction SHALL be configurable via environment variables on the server.

---

## Non-Functional Requirements

**NFR-1 Latency:** Camera-to-dashboard latency SHOULD be under 500 ms on a local network. MAR chart latency (additional MediaPipe inference) SHOULD be under 300 ms above camera latency.

**NFR-2 Bandwidth:** Camera at 30 Hz, 320×240, JPEG quality 50 uses ~1.5–2 Mbps. IMU + audio at 50 Hz add ~3 KB/s. Acceptable on any 10 Mbps+ LAN.

**NFR-3 Pi resource usage:** The Pi streamer SHALL use no local processing beyond sensor reads, JPEG encoding, and WebSocket send.

**NFR-4 Portability:** The server SHALL run on macOS and Linux with no Docker required.

**NFR-5 Graceful degradation:** If the camera is unavailable, the server and dashboard SHALL continue working for IMU + audio only.

**NFR-6 Clock discipline:** The Pi SHALL have NTP enabled. The server logs a warning if `ts_recv − ts > 2.0 s`.

**NFR-7 Data persistence:** Experiment and exercise data SHALL survive server restarts via SQLite (`app_v2/experiments.db`).

---

## Out of Scope

- Multi-Pi support
- Authentication / access control
- Live audio streaming or playback in the dashboard (WAV is post-session)
- Annotated video output with landmarks drawn
- Historical sensor data replay beyond the 10-second rolling buffer
