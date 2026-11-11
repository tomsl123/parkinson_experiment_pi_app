"""Self-contained HTML report for one run (uPlot and data inlined, works offline)."""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from ..common.clocksync import ClockSync, PingSample
from ..common.stats import histogram
from .recorder import RUNS_DIR

VENDOR = Path(__file__).parent / "static" / "vendor"
MAX_POINTS = 4000


def _series(run_dir: Path, summary: dict) -> dict:
    clock = ClockSync()
    rtt = []
    t_base = None
    with open(run_dir / "pings.csv") as f:
        for row in csv.DictReader(f):
            s = PingSample(*(int(row[k]) for k in ("t0_ns", "t1_ns", "t2_ns", "t3_ns")))
            clock.add(s)
            t_base = s.t0 if t_base is None else t_base
            rtt.append([s.t3, s.rtt_ns / 1e6])

    per: dict = {}
    with open(run_dir / "packets.csv") as f:
        for row in csv.DictReader(f):
            per.setdefault(row["name"], []).append((int(row["t_send_ns"]), int(row["t_recv_ns"])))
    first = min([v[0][1] for v in per.values() if v] + ([rtt[0][0]] if rtt else []), default=0)

    latency, hist = {}, {}
    nominal = {s["name"]: s.get("nominal_fps") for s in summary.get("streams", [])}
    for name, recs in per.items():
        stride = max(1, len(recs) // MAX_POINTS)
        pts = []
        for t_send, t_recv in recs[::stride]:
            off = clock.offset_at(t_recv)
            if off is not None:
                pts.append([round((t_recv - first) / 1e9, 4), round((t_recv - (t_send - off)) / 1e6, 3)])
        latency[name] = pts
        gaps = [(b[1] - a[1]) / 1e6 for a, b in zip(recs, recs[1:])]
        fps = nominal.get(name)
        if fps:
            hist[name] = histogram(gaps, 40, 0.0, 4000.0 / fps)
    rtt_pts = [[round((t - first) / 1e9, 4), round(v, 3)] for t, v in rtt[::max(1, len(rtt) // MAX_POINTS)]]

    cpu = []
    sys_file = run_dir / "sysinfo.jsonl"
    if sys_file.exists():
        for line in sys_file.read_text().splitlines():
            snap = json.loads(line)
            if snap.get("cpu_pct") is not None:
                cpu.append([snap.get("t_s"), snap["cpu_pct"]])
    return {"latency": latency, "hist": hist, "rtt": rtt_pts, "cpu": cpu}


def build_report_html(run_id: str) -> str:
    run_dir = RUNS_DIR / run_id
    summary = json.loads((run_dir / "summary.json").read_text())
    data = _series(run_dir, summary)
    uplot_js = (VENDOR / "uPlot.iife.min.js").read_text(encoding="utf-8")
    uplot_css = (VENDOR / "uPlot.min.css").read_text(encoding="utf-8")
    static = Path(__file__).parent / "static"
    template = (static / "report_template.html").read_text(encoding="utf-8")
    payload = json.dumps({"summary": summary, "series": data}).replace("</", "<\\/")
    template = template.replace("{{TITLE}}", html.escape(run_id))
    parts = {
        "/*UPLOT_CSS*/": uplot_css,
        "/*STYLE*/": (static / "style.css").read_text(encoding="utf-8"),
        "/*UPLOT_JS*/": uplot_js,
        "/*COMMON_JS*/": (static / "common.js").read_text(encoding="utf-8"),
        "/*DATA*/null": payload,
    }
    out = template
    for key, value in parts.items():   # each placeholder occurs once in the template
        head, sep, tail = out.partition(key)
        out = head + value + tail if sep else out
    return out


def write_report(run_id: str) -> Path:
    out = RUNS_DIR / run_id / "report.html"
    out.write_text(build_report_html(run_id), encoding="utf-8")
    return out
