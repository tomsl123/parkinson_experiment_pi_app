import math
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

# INMP441 I2S mic calibration — fixed sensitivity, no variable gain stage
# Sensitivity: -26 dBFS at 94 dB SPL (1 kHz, 1 Pa), from datasheet
_INMP441_SENSITIVITY_DBFS = -26.0
_DBFS_TO_DBSPL_OFFSET = 94.0 - _INMP441_SENSITIVITY_DBFS  # 120.0


def rms_to_dbspl(rms: float):
    """Convert normalized float32 RMS to dB SPL via INMP441 calibration.
    Returns None for zero/negative input (silence floor)."""
    if rms <= 0:
        return None
    return 20.0 * math.log10(rms) + _DBFS_TO_DBSPL_OFFSET


# ---------------------------------------------------------------------------
# SPL high-pass filter
#
# The INMP441 injects large sub-audio (<~20 Hz) DC/LF wander.  It is inaudible
# (the ear and any player high-pass it out) but it dominates a full-band
# sqrt(mean(x**2)) RMS: on real "ba" recordings ~85% of the signal power sat
# below the block rate, inflating the silence floor ~11 dB and flattening the
# dB SPL trend.  High-passing at 60 Hz — below the ~100 Hz voice fundamental,
# above the drift — removes it before RMS.  The 1 kHz calibration tone passes
# unchanged, so rms_to_dbspl stays valid after filtering.
# ---------------------------------------------------------------------------

AUDIO_SAMPLERATE   = 48000
SPL_HIGHPASS_HZ    = 60.0
SPL_HIGHPASS_ORDER = 2

# Macro smoothing of the derived dB SPL envelope.  The per-frame RMS is noisy at
# ~20 ms resolution; feature engineering wants the macro loudness shape (per-"ba"
# bursts, overall trend), not sub-100 ms jitter.  Averaging is done in the POWER
# domain (equivalent continuous level / Leq) then converted to dB — the physically
# correct time-weighting.  200 ms halves the jitter while keeping the full dynamic
# range and sits well below the ~1 s "ba" spacing, so bursts stay distinct.
SPL_SMOOTH_MS = 200.0

_HP_SOS = None


def get_spl_highpass_sos():
    """Cached second-order-sections for the SPL high-pass.  Requires scipy."""
    global _HP_SOS
    if _HP_SOS is None:
        from scipy.signal import butter
        _HP_SOS = butter(SPL_HIGHPASS_ORDER,
                         SPL_HIGHPASS_HZ / (AUDIO_SAMPLERATE / 2.0),
                         btype="high", output="sos")
    return _HP_SOS


def wav_to_dbspl_rows(wav_path, start_timestamp=None, target_hz=None, smooth_ms=None):
    """Derive [timestamp, dB SPL] rows straight from a mono WAV.

    High-passes the signal (removing the INMP441 LF drift), takes block RMS at
    ~target_hz, and converts via rms_to_dbspl.  This is the authoritative dB SPL
    for a recording: the stored per-block RMS CSV has the LF baked into each
    scalar and cannot be corrected after the fact, but the WAV is intact.

    The envelope is smoothed in the power domain (Leq-style) over ``smooth_ms`` to
    give the macro loudness shape rather than ~20 ms micro-fluctuations.

    start_timestamp: unix time of the first sample; if None it is parsed from an
    ``audio_<ts>.wav`` filename (that is exactly when capture began).
    target_hz: output rate (default AUDIO_HZ).
    smooth_ms: power-domain moving-average width (default SPL_SMOOTH_MS; 0/None
    disables smoothing).  Returns a list of [timestamp_str, dbspl_or_empty]; the
    value is "" where RMS is non-positive.
    """
    import os
    import re
    import wave

    import numpy as np
    from scipy.signal import sosfiltfilt

    if target_hz is None:
        target_hz = AUDIO_HZ
    if smooth_ms is None:
        smooth_ms = SPL_SMOOTH_MS

    with wave.open(str(wav_path), "rb") as wf:
        sr   = wf.getframerate()
        n_ch = wf.getnchannels()
        raw  = wf.readframes(wf.getnframes())

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if n_ch > 1:
        samples = samples[::n_ch]  # take first channel
    if samples.size == 0:
        return []

    if start_timestamp is None:
        m = re.search(r"audio_(\d+(?:\.\d+)?)\.wav$", os.path.basename(str(wav_path)))
        start_timestamp = float(m.group(1)) if m else 0.0

    sos = get_spl_highpass_sos()
    # sosfiltfilt needs a few times the filter length; skip on tiny clips
    if samples.size > 6 * (2 * SPL_HIGHPASS_ORDER + 1):
        samples = sosfiltfilt(sos, samples)

    block    = max(1, int(round(sr / float(target_hz))))
    n_blocks = samples.size // block
    if n_blocks == 0:
        return []
    blocks = samples[:n_blocks * block].reshape(n_blocks, block)
    power  = (blocks ** 2).mean(axis=1)  # linear power per frame

    # Macro smoothing in the power domain (Leq), then back to RMS for dB.
    win = int(round(float(smooth_ms) / 1000.0 * float(target_hz))) if smooth_ms else 0
    if win > 1:
        from scipy.ndimage import uniform_filter1d
        power = uniform_filter1d(power, win, mode="nearest")
    rms = np.sqrt(power)

    rows = []
    for i, r in enumerate(rms):
        ts = start_timestamp + (i * block) / sr
        d  = rms_to_dbspl(float(r))
        rows.append([f"{ts:.4f}", "" if d is None else round(d, 4)])
    return rows
