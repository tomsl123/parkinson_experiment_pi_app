#!/usr/bin/env python3
"""Plot a WAV waveform and its exported dB SPL CSV on a shared time axis.

Handy for eyeballing whether the dB SPL curve tracks the audio: loud "ba" bursts
should line up with tall waveform sections, silence with the low dB floor.

Usage:
    python plot_audio_compare.py --wav recordings/audio_1783349799.270.wav \
                                 --csv audio_1783349799.270.csv
    python plot_audio_compare.py --wav to_verify.wav --csv to_verify.csv --out cmp.png

The CSV is the exported audio file: two columns "timestamp,sound_pressure_dbspl".
The two panels share the x-axis (seconds from recording start) so peaks align.
Without --out the plot opens in a window; with --out it is saved as an image.
"""
import argparse
import csv as csvmod
import os
import re
import wave

import numpy as np


def load_wav(path):
    """Return (samples float64 in [-1,1], sample_rate, start_unixtime_or_None)."""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n_ch = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if n_ch > 1:
        x = x[::n_ch]  # first channel only
    m = re.search(r"audio_(\d+(?:\.\d+)?)\.wav$", os.path.basename(str(path)))
    start = float(m.group(1)) if m else None
    return x, sr, start


def envelope(x, n_buckets):
    """Min/max per bucket so a long signal draws fast without losing peaks."""
    if len(x) <= n_buckets * 2:
        t = np.arange(len(x))
        return t, x, x
    edges = np.linspace(0, len(x), n_buckets + 1, dtype=int)
    centers, mins, maxs = [], [], []
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        seg = x[a:b]
        centers.append((a + b) / 2.0)
        mins.append(seg.min())
        maxs.append(seg.max())
    return np.array(centers), np.array(mins), np.array(maxs)


def load_dbspl_csv(path):
    """Return (times_seconds_relative, dbspl_with_nan, first_unixtime)."""
    ts, db = [], []
    with open(path, newline="") as f:
        reader = csvmod.reader(f)
        header = next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            try:
                t = float(row[0])
            except ValueError:
                continue
            ts.append(t)
            try:
                db.append(float(row[1]))
            except ValueError:
                db.append(np.nan)  # blank = silence / non-positive RMS
    if not ts:
        raise SystemExit(f"No data rows found in {path}")
    ts = np.array(ts)
    return ts, np.array(db), ts[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav", required=True, help="path to the .wav recording")
    ap.add_argument("--csv", required=True, help="exported dB SPL csv (timestamp,sound_pressure_dbspl)")
    ap.add_argument("--out", default=None, help="save to this image file instead of opening a window")
    ap.add_argument("--points", type=int, default=4000, help="waveform envelope resolution (default 4000)")
    args = ap.parse_args()

    import matplotlib
    if args.out:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x, sr, wav_start = load_wav(args.wav)
    wav_dur = len(x) / sr
    csv_ts, csv_db, csv_start = load_dbspl_csv(args.csv)

    # Shared clock: prefer the absolute unix times (wav filename start vs csv
    # timestamps). Fall back to each series' own start if the wav name has no ts.
    if wav_start is not None:
        csv_t = csv_ts - wav_start
        wav_t0 = 0.0
    else:
        csv_t = csv_ts - csv_start
        wav_t0 = 0.0

    cx, cmin, cmax = envelope(x, args.points)
    wav_time = wav_t0 + cx / sr

    valid = csv_db[~np.isnan(csv_db)]
    fig, (ax_w, ax_d) = plt.subplots(2, 1, figsize=(14, 7), sharex=True,
                                     gridspec_kw={"height_ratios": [1, 1]})

    ax_w.fill_between(wav_time, cmin, cmax, color="#3a7ca5", linewidth=0)
    ax_w.set_ylabel("WAV amplitude\n(normalized)")
    ax_w.set_title(f"{os.path.basename(args.wav)}  —  {wav_dur:.1f}s @ {sr} Hz")
    ax_w.set_ylim(-max(0.01, np.abs(x).max()) * 1.1, max(0.01, np.abs(x).max()) * 1.1)
    ax_w.grid(True, alpha=0.3)
    ax_w.axhline(0, color="k", linewidth=0.5, alpha=0.4)

    ax_d.plot(csv_t, csv_db, color="#c1440e", linewidth=0.9)
    ax_d.set_ylabel("Sound pressure\n(dB SPL)")
    ax_d.set_xlabel("time (s from recording start)")
    if len(valid):
        ax_d.set_title(f"{os.path.basename(args.csv)}  —  "
                       f"min {valid.min():.1f} / mean {valid.mean():.1f} / max {valid.max():.1f} dB SPL, "
                       f"{len(csv_db)} pts")
        lo = np.floor(valid.min() / 5) * 5
        hi = np.ceil(valid.max() / 5) * 5
        ax_d.set_ylim(lo, hi)
    ax_d.grid(True, alpha=0.3)

    ax_d.set_xlim(0, max(wav_dur, csv_t.max() if len(csv_t) else wav_dur))
    fig.tight_layout()

    if args.out:
        fig.savefig(args.out, dpi=150, bbox_inches="tight")
        print(f"wrote {args.out}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
