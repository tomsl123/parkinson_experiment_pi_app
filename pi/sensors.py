import math
import threading
import time

import numpy as np


class RealIMUProvider:
    is_real = True

    def __init__(self, bus):
        from gyro import read_sensor_data
        self._bus = bus
        self._read = read_sensor_data

    def get_imu_sample(self):
        return self._read(self._bus)

    def close(self):
        self._bus.close()


class MockIMUProvider:
    is_real = False

    def __init__(self):
        self._rng = np.random.default_rng()

    def get_imu_sample(self):
        t = time.monotonic()
        rng = self._rng
        return {
            "accel_x": int(500 * math.sin(0.8 * t) + rng.normal(0, 80)),
            "accel_y": int(500 * math.sin(1.1 * t + 1.0) + rng.normal(0, 80)),
            "accel_z": int(16384 + 300 * math.sin(0.3 * t) + rng.normal(0, 50)),
            "gyro_x": int(200 * math.sin(1.5 * t) + rng.normal(0, 30)),
            "gyro_y": int(200 * math.sin(0.9 * t + 0.5) + rng.normal(0, 30)),
            "gyro_z": int(100 * math.sin(0.4 * t) + rng.normal(0, 20)),
        }

    def close(self):
        pass


class RealAudioProvider:
    is_real = True

    def __init__(self):
        import sounddevice as sd
        self._rms = 0.0
        self._lock = threading.Lock()
        self._stream = sd.InputStream(
            samplerate=48000,
            channels=1,
            dtype="float32",
            blocksize=2048,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        rms = float(np.sqrt(np.mean(indata ** 2)))
        with self._lock:
            self._rms = rms

    def get_spl_sample(self):
        with self._lock:
            return self._rms

    def close(self):
        self._stream.stop()
        self._stream.close()


class MockAudioProvider:
    is_real = False

    def __init__(self):
        self._rng = np.random.default_rng()

    def get_spl_sample(self):
        t = time.monotonic()
        base = 0.02 + 0.01 * abs(math.sin(0.2 * t))
        burst = 0.15 * max(0.0, math.sin(2.0 * t)) ** 8
        noise = abs(self._rng.normal(0, 0.005))
        return min(1.0, base + burst + noise)

    def close(self):
        pass


class RealCameraProvider:
    is_real = True

    def __init__(self):
        from picamera2 import Picamera2
        self._picam2 = Picamera2()
        config = self._picam2.create_preview_configuration(
            main={"size": (640, 480), "format": "RGB888"}
        )
        self._picam2.configure(config)
        self._picam2.start()
        time.sleep(0.5)

    def get_frame(self):
        return self._picam2.capture_array()

    def close(self):
        self._picam2.stop()
        self._picam2.close()


class MockCameraProvider:
    is_real = False

    def __init__(self):
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", (640, 360), color=(30, 30, 30))
        draw = ImageDraw.Draw(img)
        draw.text((240, 160), "No camera", fill=(120, 120, 120))
        self._frame = np.array(img)

    def get_frame(self):
        return self._frame

    def close(self):
        pass
