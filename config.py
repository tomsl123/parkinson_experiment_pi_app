import os

# Network
SERVER_HOST = os.getenv("PI_SERVER_HOST", "127.0.0.1")  # Pi sets this to server IP
INGEST_PORT = int(os.getenv("PI_INGEST_PORT", "8765"))
API_PORT    = INGEST_PORT  # FastAPI serves both WS and REST on the same port

# Sensor rates (Hz)
IMU_HZ    = 50
AUDIO_HZ  = 50
CAMERA_HZ = int(os.getenv("PI_CAMERA_HZ", "30"))

# Camera resolution — 320x240 is sufficient for MediaPipe, keeps bandwidth ~1.2 Mbps
CAMERA_WIDTH        = int(os.getenv("PI_CAMERA_WIDTH",   "320"))
CAMERA_HEIGHT       = int(os.getenv("PI_CAMERA_HEIGHT",  "240"))
CAMERA_JPEG_QUALITY = int(os.getenv("PI_CAMERA_QUALITY", "50"))   # 1–95

# Buffer / display
WINDOW_SECONDS = 10
RENDER_HZ      = 4

# Scaling (raw IMU integer → physical units)
ACCEL_SCALE = 16384.0  # LSB/g,      MPU6050 default ±2g range
GYRO_SCALE  = 131.0    # LSB/(°/s),  MPU6050 default ±250°/s range
