"""Writes a finished run to linkbench/runs/<run_id>/.

    packets.csv    one row per received data frame
    pings.csv      one row per answered ping (four timestamps, RTT, offset)
    sysinfo.jsonl  the agent's 1 Hz health messages
    summary.json   configuration, per-stream results, clock, bulk, ramp, verdict
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"


def write_run(run_id: str, summary: dict, streams: dict, pings: list, sysinfo: list,
              runs_dir: Path = RUNS_DIR) -> Path:
    d = runs_dir / run_id
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "packets.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stream", "name", "seq", "t_send_ns", "t_recv_ns", "bytes", "crc_ok",
                    "pattern_ok", "step"])
        for st in streams.values():
            for r in st.recs:
                w.writerow([st.stream, st.name, r.seq, r.t_send, r.t_recv, r.size,
                            int(r.crc_ok), "" if r.pattern_ok is None else int(r.pattern_ok),
                            r.step])
    with open(d / "pings.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t0_ns", "t1_ns", "t2_ns", "t3_ns", "rtt_ms", "offset_ms"])
        for s in pings:
            w.writerow([s.t0, s.t1, s.t2, s.t3, round(s.rtt_ns / 1e6, 4),
                        round(s.offset_ns / 1e6, 4)])
    with open(d / "sysinfo.jsonl", "w") as f:
        for snap in sysinfo:
            f.write(json.dumps(snap) + "\n")
    (d / "summary.json").write_text(json.dumps(summary, indent=2))
    return d


def list_runs(runs_dir: Path = RUNS_DIR) -> list[dict]:
    out = []
    if not runs_dir.exists():
        return out
    for d in sorted(runs_dir.iterdir(), reverse=True):
        f = d / "summary.json"
        if f.exists():
            try:
                out.append(json.loads(f.read_text()))
            except json.JSONDecodeError:
                pass
    return out


def load_run(run_id: str, runs_dir: Path = RUNS_DIR) -> dict:
    return json.loads((runs_dir / run_id / "summary.json").read_text())
