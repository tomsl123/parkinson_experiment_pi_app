"""Test stream definitions and presets, modelled on the MOVES recording setup.

A stream produces `rate_hz / batch` frames per second; each frame carries `batch`
samples of `sample_bytes` (IMU: 6 × int16 = 12 bytes). Camera frames are blobs of a
JPEG-like size.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class StreamSpec:
    name: str
    stream: int            # stream id in the frame header (1..249)
    kind: str              # imu | audio | camera
    rate_hz: float         # samples per second
    sample_bytes: int      # bytes per sample
    batch: int = 1         # samples per frame
    live: bool = True      # judged against the live-latency threshold

    @property
    def frame_rate(self) -> float:
        return self.rate_hz / max(1, self.batch)

    @property
    def payload_bytes(self) -> int:
        return self.sample_bytes * max(1, self.batch)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StreamSpec":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def imu_left(rate=100.0, batch=1):
    return StreamSpec("imu_left", 1, "imu", rate, 12, batch)


def imu_right(rate=100.0, batch=1):
    return StreamSpec("imu_right", 2, "imu", rate, 12, batch)


def audio_level(rate=50.0):
    return StreamSpec("audio_level", 3, "audio", rate, 4)


def camera(fps=15.0, frame_bytes=8000):
    return StreamSpec("camera", 4, "camera", fps, frame_bytes)


@dataclass
class RampSpec:
    """Raise camera fps step by step while the IMU streams keep running."""
    fps_steps: list = field(default_factory=lambda: [5, 10, 15, 20, 30, 45, 60, 90])
    frame_bytes: int = 12000
    step_s: float = 5.0
    max_loss_pct: float = 1.0
    max_p95_ms: float = 200.0


@dataclass
class Profile:
    name: str
    label: str
    streams: list
    duration_s: float = 30.0
    bulk_bytes: int = 0          # > 0: run a bulk transfer instead of / after streams
    ramp: Optional[RampSpec] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def presets() -> dict:
    return {
        "ping": Profile("ping", "Ping only (clock sync and RTT)", [], 20.0),
        "imu": Profile("imu", "Two leg IMUs, 100 Hz each", [imu_left(), imu_right()], 30.0),
        "full": Profile("full", "Full session: 2 IMUs, audio level, camera 15 fps",
                        [imu_left(), imu_right(), audio_level(), camera()], 60.0),
        "camera_stress": Profile("camera_stress", "Camera stress: 30 fps, 15 kB frames + IMUs",
                                 [imu_left(), imu_right(), camera(30.0, 15000)], 30.0),
        "bulk": Profile("bulk", "Bulk transfer: 6 MB (about 1 min of WAV)", [], 0.0,
                        bulk_bytes=6 * 1024 * 1024),
        "ramp": Profile("ramp", "Ramp: camera 5 to 90 fps until the link breaks",
                        [imu_left(), imu_right()], 0.0, ramp=RampSpec()),
    }


# Draft pass/fail thresholds (editable from the CLI / dashboard)
DEFAULT_THRESHOLDS = {
    "max_loss_pct": 0.1,
    "max_rate_error_pct": 2.0,
    "max_p95_latency_ms": 100.0,
    "max_integrity_errors": 0,
}
