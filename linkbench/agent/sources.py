"""Payload sources: synthetic test pattern, or the real Pi sensors.

Real mode reuses the prototype's providers where they fit (`pi/sensors.py`
RealCameraProvider, `pi/streamer.py` SharedAudioProvider). IMUs are read directly with
one 14-byte I2C block read per sample (MPU6050 at 0x68 = left, 0x69 = right), sampled in
a background thread at the stream's rate so that batched frames still carry evenly spaced
samples. Any sensor that fails to start falls back to the synthetic pattern, and the
fallback is reported to the host.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from ..common.packet import FLAG_PATTERN, pattern
from ..common.profiles import StreamSpec

log = logging.getLogger("linkbench.agent")

REPO_ROOT = Path(__file__).resolve().parents[2]


class Source:
    label = "synthetic"
    flags = FLAG_PATTERN

    async def payload(self, spec: StreamSpec, seq: int) -> bytes:
        return pattern(spec.stream, seq, spec.payload_bytes)

    def close(self) -> None:
        pass


SYNTHETIC = Source()


# ── Real sensors ────────────────────────────────────────────────────────────────

MPU_PWR_MGMT_1 = 0x6B
MPU_ACCEL_XOUT_H = 0x3B
IMU_ADDRS = {"imu_left": 0x68, "imu_right": 0x69}


class ImuSource(Source):
    flags = 0

    def __init__(self, bus, addr: int, rate_hz: float) -> None:
        self.label = f"MPU6050 @0x{addr:02x}"
        self.bus, self.addr = bus, addr
        bus.write_byte_data(addr, MPU_PWR_MGMT_1, 0)
        self.read()                                   # raises if absent
        self.samples: deque = deque(maxlen=1000)
        self.period = 1.0 / rate_hz
        self.running = True
        threading.Thread(target=self._sampler, daemon=True).start()

    def read(self) -> bytes:
        raw = bytes(self.bus.read_i2c_block_data(self.addr, MPU_ACCEL_XOUT_H, 14))
        ax, ay, az, _temp, gx, gy, gz = struct.unpack(">7h", raw)
        return struct.pack("<6h", ax, ay, az, gx, gy, gz)

    def _sampler(self) -> None:
        nxt = time.perf_counter()
        while self.running:
            try:
                self.samples.append(self.read())
            except OSError:
                self.samples.append(b"\x00" * 12)
            nxt += self.period
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -5 * self.period:
                nxt = time.perf_counter()

    async def payload(self, spec: StreamSpec, seq: int) -> bytes:
        out = []
        for _ in range(spec.batch):
            out.append(self.samples.popleft() if self.samples else b"\x00" * 12)
        return b"".join(out)

    def close(self) -> None:
        self.running = False


class AudioSource(Source):
    flags = 0

    def __init__(self) -> None:
        sys.path.insert(0, str(REPO_ROOT))
        from pi.streamer import SharedAudioProvider
        self.provider = SharedAudioProvider()
        self.label = "INMP441 via SharedAudioProvider (RMS)"

    async def payload(self, spec: StreamSpec, seq: int) -> bytes:
        return struct.pack("<f", self.provider.get_spl_sample())

    def close(self) -> None:
        self.provider.close()


class CameraSource(Source):
    flags = 0

    def __init__(self, width: int = 320, height: int = 240, quality: int = 50) -> None:
        sys.path.insert(0, str(REPO_ROOT))
        import cv2
        from pi.sensors import RealCameraProvider
        self.cv2 = cv2
        self.cam = RealCameraProvider()
        self.size, self.quality = (width, height), quality
        self.label = f"OV5647 JPEG {width}x{height} q{quality}"

    def _grab(self) -> bytes:
        frame = self.cam.get_frame()
        small = self.cv2.resize(frame, self.size)
        ok, buf = self.cv2.imencode(".jpg", small, [self.cv2.IMWRITE_JPEG_QUALITY, self.quality])
        return bytes(buf) if ok else b""

    async def payload(self, spec: StreamSpec, seq: int) -> bytes:
        return await asyncio.to_thread(self._grab)

    def close(self) -> None:
        self.cam.close()


class RealSensors:
    """Opens each real sensor once per agent process and maps streams to them."""

    def __init__(self) -> None:
        self._by_kind: dict = {}
        self._bus = None
        self.errors: dict = {}

    def _bus_or_none(self):
        if self._bus is None:
            from smbus2 import SMBus
            self._bus = SMBus(1)
        return self._bus

    def source_for(self, spec: StreamSpec) -> Source:
        key = spec.name if spec.kind == "imu" else spec.kind
        if key in self._by_kind:
            return self._by_kind[key]
        try:
            if spec.kind == "imu":
                src = ImuSource(self._bus_or_none(), IMU_ADDRS.get(spec.name, 0x68), spec.rate_hz)
            elif spec.kind == "audio":
                src = AudioSource()
            elif spec.kind == "camera":
                src = CameraSource()
            else:
                raise ValueError(f"no real source for kind '{spec.kind}'")
        except Exception as e:  # sensor missing or library absent → synthetic fallback
            self.errors[key] = str(e)
            log.warning("real %s unavailable (%s) – using synthetic pattern", key, e)
            src = SYNTHETIC
        self._by_kind[key] = src
        return src

    def describe(self, spec: StreamSpec) -> str:
        src = self.source_for(spec)
        if src is SYNTHETIC:
            key = spec.name if spec.kind == "imu" else spec.kind
            return f"synthetic (fallback: {self.errors.get(key, 'unavailable')})"
        return src.label

    def close(self) -> None:
        for src in self._by_kind.values():
            src.close()
        self._by_kind.clear()


_real: Optional[RealSensors] = None


def real_sensors() -> RealSensors:
    global _real
    if _real is None:
        _real = RealSensors()
    return _real
