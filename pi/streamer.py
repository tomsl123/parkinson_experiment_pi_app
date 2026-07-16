"""Pi-side WebSocket streamer.

Reads IMU, audio SPL, and camera data from sensors and sends them to the server
over a single WebSocket connection at configurable rates.  Also receives control
commands (start_wav / stop_wav) from the server and handles WAV recording +
upload.

Usage (on the Pi):
    export PI_SERVER_HOST=<server-ip>
    python pi/streamer.py

Audio note: a single sounddevice InputStream is opened for the entire lifetime of
the streamer.  The same callback computes SPL (for streaming) AND optionally feeds
a WAV queue (for recording).  This avoids opening a second ALSA stream — only one
exclusive capture stream is allowed per device on most Raspberry Pi OS configs.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

# Ensure project root is on path so config and sensors are importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    SERVER_HOST, INGEST_PORT,
    IMU_HZ, AUDIO_HZ, CAMERA_HZ,
    CAMERA_WIDTH, CAMERA_HEIGHT, CAMERA_JPEG_QUALITY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

WS_URL = f"ws://{SERVER_HOST}:{INGEST_PORT}/stream"

AUDIO_SAMPLERATE = 48000
AUDIO_CHANNELS   = 1
AUDIO_BLOCKSIZE  = 2048


# ---------------------------------------------------------------------------
# Shared audio provider — single ALSA stream for SPL + WAV
# ---------------------------------------------------------------------------

class SharedAudioProvider:
    """Opens ONE sounddevice InputStream.  Provides:
    - get_spl_sample(): RMS amplitude (same API as RealAudioProvider)
    - WAV recording: call start_wav_capture() / stop_wav_capture()

    Having a single stream avoids 'Device unavailable' errors when WAV
    recording starts while SPL streaming is already running.
    """

    def __init__(self) -> None:
        import sounddevice as sd

        self._lock   = threading.Lock()
        self._rms    = 0.0

        # WAV capture state — only set while recording is active
        self._wav_queue: Optional[queue.Queue] = None

        def _callback(indata: np.ndarray, frames: int, time_info, status) -> None:
            # 1. Compute RMS for SPL streaming
            rms = float(np.sqrt(np.mean(indata ** 2)))
            with self._lock:
                self._rms = rms
            # 2. If WAV recording is active, enqueue a copy of the raw samples
            q = self._wav_queue  # read atomically (GIL-protected assignment)
            if q is not None:
                q.put(indata.copy())

        self._stream = sd.InputStream(
            samplerate=AUDIO_SAMPLERATE,
            channels=AUDIO_CHANNELS,
            dtype="float32",
            blocksize=AUDIO_BLOCKSIZE,
            callback=_callback,
        )
        self._stream.start()
        log.info("SharedAudioProvider: ALSA stream started")

    def get_spl_sample(self) -> float:
        with self._lock:
            return self._rms

    def start_wav_capture(self) -> queue.Queue:
        """Attach a queue to the callback; returns it for the caller to drain."""
        q: queue.Queue = queue.Queue()
        self._wav_queue = q
        return q

    def stop_wav_capture(self) -> None:
        """Detach the queue; the callback stops enqueuing."""
        self._wav_queue = None

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


# ---------------------------------------------------------------------------
# NTP check (AC-3b)
# ---------------------------------------------------------------------------

def _check_ntp() -> None:
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized"],
            capture_output=True, text=True, timeout=5,
        )
        if "yes" not in result.stdout:
            log.warning("Clock not NTP-synced — timestamps may be unreliable")
        else:
            log.info("NTP sync: OK")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.warning("Could not check NTP sync (timedatectl not available)")


# ---------------------------------------------------------------------------
# Sensor init helpers (AC-5)
# ---------------------------------------------------------------------------

def _try_init(factory, name: str):
    try:
        provider = factory()
        log.info("%s provider: OK", name)
        return provider
    except Exception as exc:
        log.warning("%s provider unavailable (%s) — skipping", name, exc)
        return None


# ---------------------------------------------------------------------------
# WAV recording state (shared between control_loop and record_wav task)
# ---------------------------------------------------------------------------

# _wav_stop_event is created fresh for each recording session so there is no
# race between a leftover set() from a prior session and the new task starting.
_wav_stop_event: Optional[threading.Event] = None
_wav_recording  = False  # only mutated from the asyncio event loop

# The shared audio provider (set in main(), used by _record_wav_task)
_shared_audio: Optional[SharedAudioProvider] = None


async def _record_wav_task(server_host: str, server_port: int,
                           stop_event: threading.Event) -> None:
    """Async task: record WAV to a local file and upload when done."""
    global _wav_recording

    import soundfile as sf
    import requests as req

    _wav_recording = True

    ts_start = time.time()
    filename = f"audio_{ts_start:.3f}.wav"

    log.info("WAV recording started → %s (stop_event already set? %s)",
             filename, stop_event.is_set())

    try:
        # Attach WAV capture to the existing audio stream (no second ALSA open)
        if _shared_audio is None:
            log.error("SharedAudioProvider not initialised — cannot record WAV")
            return

        wav_queue = _shared_audio.start_wav_capture()
        chunks_written = 0

        try:
            with sf.SoundFile(filename, mode="w",
                              samplerate=AUDIO_SAMPLERATE,
                              channels=AUDIO_CHANNELS,
                              subtype="PCM_16") as wav_file:
                loop = asyncio.get_running_loop()
                while not stop_event.is_set():
                    try:
                        chunk = await loop.run_in_executor(
                            None, lambda: wav_queue.get(timeout=0.1)
                        )
                        wav_file.write(chunk)
                        chunks_written += 1
                    except queue.Empty:
                        pass

                # Detach the queue so the callback stops enqueuing
                _shared_audio.stop_wav_capture()

                # Drain any remaining chunks already in the queue
                while True:
                    try:
                        wav_file.write(wav_queue.get_nowait())
                        chunks_written += 1
                    except queue.Empty:
                        break

        except Exception:
            _shared_audio.stop_wav_capture()
            raise

        log.info("WAV recording finalised: %s (%d chunks)", filename, chunks_written)

        # Upload to server in a thread (non-blocking for the event loop)
        def _upload():
            url = f"http://{server_host}:{server_port}/wav"
            try:
                with open(filename, "rb") as fh:
                    resp = req.post(
                        url,
                        data=fh,
                        headers={
                            "Content-Type": "audio/wav",
                            "Content-Disposition": f'attachment; filename="{filename}"',
                        },
                        timeout=30,
                    )
                log.info("WAV upload: %s (HTTP %d)", filename, resp.status_code)
            except Exception as exc:
                log.error("WAV upload failed: %s", exc)

        await asyncio.get_running_loop().run_in_executor(None, _upload)

    except Exception as exc:
        log.error("WAV recording error: %s", exc)
    finally:
        _wav_recording = False


# ---------------------------------------------------------------------------
# Send loops
# ---------------------------------------------------------------------------

async def _imu_loop(ws, imu) -> None:
    if imu is None:
        return
    period = 1.0 / IMU_HZ
    while True:
        try:
            ts = time.time()                      # stamp BEFORE read (AC-3)
            sample = imu.get_imu_sample()
            frame  = {"type": "imu", "ts": ts, **sample}
            await ws.send(json.dumps(frame))
        except Exception as exc:
            log.debug("IMU send error: %s", exc)
            raise
        await asyncio.sleep(period)


async def _audio_loop(ws, audio) -> None:
    """Stream SPL values.  *audio* must be a SharedAudioProvider or any object
    with a get_spl_sample() method."""
    if audio is None:
        return
    period = 1.0 / AUDIO_HZ
    while True:
        try:
            ts  = time.time()
            spl = audio.get_spl_sample()
            await ws.send(json.dumps({"type": "audio", "ts": ts, "spl": spl}))
        except Exception as exc:
            log.debug("Audio send error: %s", exc)
            raise
        await asyncio.sleep(period)


async def _camera_loop(ws, cam) -> None:
    if cam is None:
        return
    import cv2

    period = 1.0 / CAMERA_HZ
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, CAMERA_JPEG_QUALITY]
    while True:
        try:
            ts        = time.time()
            frame_rgb = cam.get_frame()
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            resized   = cv2.resize(frame_bgr, (CAMERA_WIDTH, CAMERA_HEIGHT))
            _, buf    = cv2.imencode(".jpg", resized, encode_params)
            jpeg_b64  = base64.b64encode(buf).decode()
            await ws.send(json.dumps({"type": "camera", "ts": ts, "jpeg": jpeg_b64}))
        except Exception as exc:
            log.debug("Camera send error: %s", exc)
            raise
        await asyncio.sleep(period)


async def _control_loop(ws) -> None:
    """Listen for control commands sent by the server (AC-14, AC-15)."""
    global _wav_recording, _wav_stop_event

    async for raw in ws:
        try:
            msg = json.loads(raw)
        except Exception:
            log.warning("Invalid control message: %s", raw[:80])
            continue

        cmd = msg.get("cmd")
        log.info("Received control command: %s", cmd)

        if cmd == "start_wav":
            if _wav_recording:
                log.warning("WAV already recording, ignoring start_wav")
            else:
                # Create a fresh stop event BEFORE creating the task so the
                # task sees a clean (unset) event from the very first iteration.
                _wav_stop_event = threading.Event()
                asyncio.create_task(
                    _record_wav_task(SERVER_HOST, INGEST_PORT, _wav_stop_event)
                )

        elif cmd == "stop_wav":
            if _wav_stop_event is not None:
                _wav_stop_event.set()
            else:
                log.warning("stop_wav received but no active recording")

        else:
            log.warning("Unknown command: %s", cmd)


# ---------------------------------------------------------------------------
# Main reconnect loop
# ---------------------------------------------------------------------------

async def _run(imu, audio, cam) -> None:
    import websockets

    attempt = 0
    while True:
        try:
            log.info("Connecting to %s …", WS_URL)
            async with websockets.connect(WS_URL) as ws:
                attempt = 0
                log.info("Connected")

                loops = [_control_loop(ws)]
                if imu is not None:
                    loops.append(_imu_loop(ws, imu))
                if audio is not None:
                    loops.append(_audio_loop(ws, audio))
                if cam is not None:
                    loops.append(_camera_loop(ws, cam))

                await asyncio.gather(*loops)

        except Exception as exc:
            delay = min(30, 2 ** attempt)
            log.warning("Disconnected: %s. Retrying in %d s", exc, delay)
            await asyncio.sleep(delay)
            attempt += 1


def main() -> None:
    global _shared_audio

    _check_ntp()

    imu = cam = None

    # IMU
    try:
        from smbus2 import SMBus
        from gyro import MPU6050_ADDR, PWR_MGMT_1
        bus = SMBus(1)
        bus.write_byte_data(MPU6050_ADDR, PWR_MGMT_1, 0)
        from pi.sensors import RealIMUProvider
        imu = _try_init(lambda: RealIMUProvider(bus), "IMU")
    except Exception as exc:
        log.warning("IMU init failed: %s", exc)

    # Audio — use SharedAudioProvider (replaces RealAudioProvider) so we can
    # share the single ALSA stream between SPL streaming and WAV recording.
    audio: Optional[SharedAudioProvider] = None
    try:
        audio = SharedAudioProvider()
        _shared_audio = audio
        log.info("Audio provider: OK (shared stream)")
    except Exception as exc:
        log.warning("Audio provider unavailable (%s) — skipping", exc)

    # Camera
    try:
        from pi.sensors import RealCameraProvider
        cam = _try_init(RealCameraProvider, "Camera")
    except Exception as exc:
        log.warning("Camera init failed: %s", exc)

    if imu is None and audio is None and cam is None:
        log.error("No sensors available — exiting")
        sys.exit(1)

    asyncio.run(_run(imu, audio, cam))


if __name__ == "__main__":
    main()
