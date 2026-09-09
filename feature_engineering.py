"""
Build a session x feature table from the raw sensor CSVs in data/.

Pipeline: data/{imu,audio,mouth}_<timestamp>.csv (+ optional session_meta.csv)
          -> extract_features() -> feature_table.csv
See README.md for the data layout and feature definitions.
"""

import glob
import os
import re

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

import config

DATA_DIR = "data"
OUT_TABLE = "feature_table.csv"
OUT_PLOT = "feature_summary.png"

STEP_HEIGHT_G = 1.2       # accel_z threshold for a heel-strike, just above resting gravity
ACTIVITY_THRESHOLD_G = 1.05  # accel magnitude threshold for "moving"
# Speech vs silence uses a per-recording adaptive dB SPL threshold:
#   floor(p10) + VOCAL_ACTIVITY_FRACTION * (loud(p90) - floor(p10)).
# Adapting per recording keeps it robust to patient loudness and mic distance,
# which a fixed dB level would not be.
VOCAL_ACTIVITY_FRACTION = 0.35
MOUTH_HEIGHT_FRACTION = 0.6  # relative threshold for a mouth-opening peak
BA_WINDOW_S = 0.3         # +/- window around each step to look for a "Ba" loudness peak

VIDEO_FEATURES = [
    "mean_mouth_opening", "mouth_opening_rate", "opening_variability", "opening_trend",
]


def sampling_rate(timestamps):
    """Samples per second, derived from the median gap between timestamps."""
    return 1.0 / np.median(np.diff(timestamps))


def load_session_meta(data_dir=DATA_DIR):
    """session -> distance_m, from data/session_meta.csv (dashboard will write this later)."""
    path = os.path.join(data_dir, "session_meta.csv")
    if not os.path.exists(path):
        return {}
    df_meta = pd.read_csv(path, dtype={"session": str})
    return dict(zip(df_meta["session"], df_meta["distance_m"]))


def discover_sessions(data_dir=DATA_DIR):
    """Pair imu/audio/mouth CSVs that share the same <timestamp> filename suffix."""
    session_meta = load_session_meta(data_dir)
    sessions = {}
    for imu_path in sorted(glob.glob(os.path.join(data_dir, "imu_*.csv"))):
        match = re.match(r"imu_(.+)\.csv$", os.path.basename(imu_path))
        session_id = match.group(1)
        audio_path = os.path.join(data_dir, f"audio_{session_id}.csv")
        mouth_path = os.path.join(data_dir, f"mouth_{session_id}.csv")
        sessions[session_id] = {
            "imu": imu_path,
            "audio": audio_path if os.path.exists(audio_path) else None,
            "mouth": mouth_path if os.path.exists(mouth_path) else None,
            "distance_m": session_meta.get(session_id),
        }
    return sessions


def detect_steps(df_imu):
    """Step timestamps (seconds, same clock as the raw data) and the IMU sample rate."""
    accel_z = df_imu["accel_z(g)"].values
    timestamps = df_imu["timestamp"].values
    fs = sampling_rate(timestamps)

    min_distance_s = 0.5  # matches the PDF's 25-samples-at-50Hz spacing
    peaks, _ = find_peaks(
        accel_z,
        height=STEP_HEIGHT_G,
        distance=max(1, round(min_distance_s * fs)),
    )
    return timestamps[peaks], fs


def compute_accel_features(df_imu, step_timestamps, fs):
    accel = df_imu[["accel_x(g)", "accel_y(g)", "accel_z(g)"]].values
    timestamps = df_imu["timestamp"].values
    duration_s = timestamps[-1] - timestamps[0]

    step_count = len(step_timestamps)
    cadence = step_count / duration_s * 60 if duration_s > 0 else np.nan
    intervals_s = np.diff(step_timestamps)
    gait_regularity = np.std(intervals_s) if len(intervals_s) > 0 else np.nan

    magnitude = np.sqrt((accel ** 2).sum(axis=1))
    activity_ratio = (magnitude > ACTIVITY_THRESHOLD_G).mean()

    return {
        "step_count": step_count,
        "cadence": cadence,
        "gait_regularity": gait_regularity,
        "activity_ratio": activity_ratio,
        "imu_sample_rate_hz": fs,
    }


def _loudness_dbspl(df_audio):
    """(timestamps, dB SPL) from an audio df, non-finite rows dropped.

    Prefers the WAV-derived, smoothed ``sound_pressure_dbspl`` column; falls back
    to a legacy raw-RMS ``spl`` column (converted via config.rms_to_dbspl)."""
    ts = pd.to_numeric(df_audio["timestamp"], errors="coerce").to_numpy(dtype=float)
    if "sound_pressure_dbspl" in df_audio.columns:
        db = pd.to_numeric(df_audio["sound_pressure_dbspl"], errors="coerce").to_numpy(dtype=float)
    elif "spl" in df_audio.columns:
        rms = pd.to_numeric(df_audio["spl"], errors="coerce").to_numpy(dtype=float)
        db = np.array([config.rms_to_dbspl(r) if np.isfinite(r) and r > 0 else np.nan
                       for r in rms], dtype=float)
    else:
        return np.array([]), np.array([])
    mask = np.isfinite(ts) & np.isfinite(db)
    return ts[mask], db[mask]


def _vocal_threshold(db):
    """Adaptive speech/silence cut for one recording (dB SPL)."""
    if db.size == 0:
        return np.nan
    lo, hi = np.percentile(db, 10), np.percentile(db, 90)
    return lo + VOCAL_ACTIVITY_FRACTION * (hi - lo)


def compute_voice_features(df_audio):
    keys = ["mean_loudness", "vocal_activity_ratio", "loudness_variability",
            "loudness_trend", "audio_sample_rate_hz"]
    if df_audio is None:
        return {k: np.nan for k in keys}

    timestamps, db = _loudness_dbspl(df_audio)
    if db.size == 0:
        return {k: np.nan for k in keys}
    time_axis = timestamps - timestamps[0]
    threshold = _vocal_threshold(db)

    return {
        "mean_loudness": db.mean(),                                    # dB SPL
        "vocal_activity_ratio": float((db > threshold).mean()),        # fraction phonating
        "loudness_variability": db.std(),                             # dB
        "loudness_trend": np.polyfit(time_axis, db, 1)[0] if db.size > 1 else np.nan,  # dB/s
        "audio_sample_rate_hz": sampling_rate(timestamps) if db.size > 1 else np.nan,
    }


def compute_gyro_features(df_imu):
    gyro = df_imu[["gyro_x(dps)", "gyro_y(dps)", "gyro_z(dps)"]].values
    magnitude = np.sqrt((gyro ** 2).sum(axis=1))
    return {
        "mean_rotation": magnitude.mean(),
        "rotation_variability": magnitude.std(),
    }


def compute_video_features(df_mouth):
    keys = VIDEO_FEATURES + ["video_sample_rate_hz"]
    if df_mouth is None:
        return {k: np.nan for k in keys}

    # Support both new (vertical/horizontal) and legacy (mar) CSV shapes
    if "vertical" in df_mouth.columns and "horizontal" in df_mouth.columns:
        denom = df_mouth["horizontal"].replace(0, float("nan"))
        mar = (df_mouth["vertical"] / denom).values
    else:
        mar = df_mouth["mar"].values  # legacy: unitless ratio
    timestamps = df_mouth["timestamp"].values
    fps = sampling_rate(timestamps)
    duration_s = timestamps[-1] - timestamps[0]
    time_axis = timestamps - timestamps[0]

    peaks, _ = find_peaks(
        mar,
        height=mar.mean() * MOUTH_HEIGHT_FRACTION,
        distance=max(1, round(0.2 * fps)),  # matches the PDF's 5-frames-at-25fps spacing
    )
    mouth_opening_rate = len(peaks) / duration_s * 60 if duration_s > 0 else np.nan
    opening_trend = np.polyfit(time_axis, mar, 1)[0] if len(mar) > 1 else np.nan

    return {
        "mean_mouth_opening": mar.mean(),
        "mouth_opening_rate": mouth_opening_rate,
        "opening_variability": mar.std(),
        "opening_trend": opening_trend,
        "video_sample_rate_hz": fps,
    }


def compute_distance_features(distance_m, duration_s, step_count):
    """Walking speed and step length; NaN if no distance was entered for this session."""
    if distance_m is None or pd.isna(distance_m):
        return {"walking_speed_mps": np.nan, "step_length_m": np.nan}
    return {
        "walking_speed_mps": distance_m / duration_s if duration_s > 0 else np.nan,
        "step_length_m": distance_m / step_count if step_count > 0 else np.nan,
    }


def compute_ba_vocalization(step_timestamps, df_audio):
    """For each step, look for a loud 'Ba' near it (loudness above the adaptive
    threshold within +/- BA_WINDOW_S).  Loudness is dB SPL from the WAV."""
    if df_audio is None or len(step_timestamps) == 0:
        return {"ba_vocalization_ratio": np.nan, "mean_ba_loudness": np.nan}

    audio_ts, db = _loudness_dbspl(df_audio)
    if db.size == 0:
        return {"ba_vocalization_ratio": np.nan, "mean_ba_loudness": np.nan}
    threshold = _vocal_threshold(db)

    peak_loudnesses = []
    vocalized_count = 0
    for step_ts in step_timestamps:
        window = (audio_ts >= step_ts - BA_WINDOW_S) & (audio_ts <= step_ts + BA_WINDOW_S)
        window_db = db[window]
        if window_db.size == 0:
            continue
        peak = window_db.max()  # smoothed envelope: the window max is the "Ba" level
        if peak > threshold:
            vocalized_count += 1
            peak_loudnesses.append(peak)

    return {
        "ba_vocalization_ratio": vocalized_count / len(step_timestamps),
        "mean_ba_loudness": float(np.mean(peak_loudnesses)) if peak_loudnesses else np.nan,
    }


def extract_features(files):
    df_imu = pd.read_csv(files["imu"])
    df_audio = pd.read_csv(files["audio"]) if files["audio"] else None
    df_mouth = pd.read_csv(files["mouth"]) if files["mouth"] else None
    distance_m = files["distance_m"]

    step_timestamps, fs = detect_steps(df_imu)
    duration_s = df_imu["timestamp"].values[-1] - df_imu["timestamp"].values[0]

    features = {}
    features.update(compute_accel_features(df_imu, step_timestamps, fs))
    features.update(compute_voice_features(df_audio))
    features.update(compute_gyro_features(df_imu))
    features.update(compute_video_features(df_mouth))
    features.update(compute_distance_features(distance_m, duration_s, len(step_timestamps)))
    features.update(compute_ba_vocalization(step_timestamps, df_audio))
    features["distance_m"] = distance_m

    return features


def build_feature_table(data_dir=DATA_DIR):
    sessions = discover_sessions(data_dir)
    rows = {session_id: extract_features(files) for session_id, files in sessions.items()}
    df_features = pd.DataFrame(rows).T
    df_features.index.name = "session"
    return df_features


def plot_feature_summary(df_features, out_path=OUT_PLOT):
    import matplotlib.pyplot as plt

    sessions = list(df_features.index)
    colors = plt.cm.viridis(np.linspace(0.15, 0.85, len(sessions))) if len(sessions) > 1 else ["#6fd49a"]

    panels = {
        "Accelerometer": ["step_count", "cadence", "gait_regularity", "activity_ratio"],
        "Voice": ["mean_loudness", "vocal_activity_ratio", "loudness_variability", "loudness_trend"],
        "Gyroscope": ["mean_rotation", "rotation_variability"],
        "Video": VIDEO_FEATURES,
    }

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    for ax, (title, feats) in zip(axes.flat, panels.items()):
        if title == "Video" and df_features[feats].isna().all().all():
            ax.text(0.5, 0.5, "no mouth-opening data yet", ha="center", va="center",
                     transform=ax.transAxes, fontsize=12, color="gray")
            ax.set_title(title)
            ax.set_xticks([])
            ax.set_yticks([])
            continue

        x = np.arange(len(feats))
        width = 0.8 / max(len(sessions), 1)
        for i, session in enumerate(sessions):
            values = df_features.loc[session, feats].astype(float)
            ax.bar(x + i * width, values, width=width, label=session, color=colors[i % len(colors)])
        ax.set_xticks(x + width * (len(sessions) - 1) / 2)
        ax.set_xticklabels(feats, rotation=20, ha="right")
        ax.set_title(title)
        if any(f in feats for f in ("gait_regularity", "loudness_trend")):
            ax.set_xlabel("closer to zero = improvement, where applicable")
        if len(sessions) > 1:
            ax.legend(fontsize=8)

    plt.suptitle("Feature summary", fontsize=18)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    df_features = build_feature_table()
    print(df_features.T)
    df_features.to_csv(OUT_TABLE)
    plot_feature_summary(df_features)
    print(f"\nWrote {OUT_TABLE} and {OUT_PLOT}")


if __name__ == "__main__":
    main()
