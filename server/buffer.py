"""Thread-safe in-memory circular buffer for sensor frames."""
from __future__ import annotations

from collections import deque
from threading import Lock
from typing import Optional


class SensorBuffer:
    """Stores the last `maxlen` frames for IMU, audio, and NMH streams.

    Camera frames are stored as latest-frame-only (no history) to avoid
    accumulating ~90 MB of JPEG data over a 10-second window at 15 Hz.
    """

    def __init__(self, maxlen: int) -> None:
        self._lock     = Lock()
        self._imu:   deque[dict] = deque(maxlen=maxlen)
        self._audio: deque[dict] = deque(maxlen=maxlen)
        self._nhm:   deque[dict] = deque(maxlen=maxlen)  # {"ts": float, "nhm": float|None}

        # Latest camera frame only
        self._camera:    Optional[bytes] = None
        self._camera_ts: Optional[float] = None

        # Monotonically updated whenever any frame arrives
        self._last_seen: Optional[float] = None

    # ── Writers ──────────────────────────────────────────────────────────────

    def push_imu(self, frame: dict) -> None:
        with self._lock:
            self._imu.append(frame)
            if self._last_seen is None or frame["ts"] > self._last_seen:
                self._last_seen = frame["ts"]

    def push_audio(self, frame: dict) -> None:
        with self._lock:
            self._audio.append(frame)
            if self._last_seen is None or frame["ts"] > self._last_seen:
                self._last_seen = frame["ts"]

    def push_nhm(self, ts: float, nhm: Optional[float]) -> None:
        with self._lock:
            self._nhm.append({"ts": ts, "nhm": nhm})

    def push_camera(self, jpeg_bytes: bytes, ts: float) -> None:
        with self._lock:
            self._camera    = jpeg_bytes
            self._camera_ts = ts
            if self._last_seen is None or ts > self._last_seen:
                self._last_seen = ts

    # ── Readers ──────────────────────────────────────────────────────────────

    def get_imu(self, since: float = 0.0) -> list[dict]:
        with self._lock:
            return [f for f in self._imu if f["ts"] > since]

    def get_audio(self, since: float = 0.0) -> list[dict]:
        with self._lock:
            return [f for f in self._audio if f["ts"] > since]

    def get_nhm(self, since: float = 0.0) -> list[dict]:
        with self._lock:
            return [f for f in self._nhm if f["ts"] > since]

    def get_camera(self) -> tuple[Optional[bytes], Optional[float]]:
        with self._lock:
            return self._camera, self._camera_ts

    def last_seen(self) -> Optional[float]:
        with self._lock:
            return self._last_seen
