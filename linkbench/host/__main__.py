"""linkbench host (runs on the computer).

    python -m linkbench.host serve                                   # dashboard on http://127.0.0.1:8770
    python -m linkbench.host run --transport ws --addr 10.42.0.1 --link ap --profile full
    python -m linkbench.host list
    python -m linkbench.host report <run_id>                          # self-contained HTML report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from ..common.profiles import presets
from .recorder import RUNS_DIR, list_runs
from .session import BenchSession, RunConfig


def _print_summary(s: dict) -> None:
    print(f"\nrun {s['run_id']}  -  {'PASS' if s['passed'] else 'FAIL'}"
          + (f"  ({s['error']})" if s.get("error") else ""))
    c = s["clock"]
    print(f"clock: offset {c['offset_ms']} ms, drift {c['drift_ppm']} ppm, "
          f"RTT p50 {c['rtt_ms_p50']} / p95 {c['rtt_ms_p95']} ms, ping loss {c['ping_loss_pct']} %")
    if s["streams"]:
        print(f"{'stream':<12}{'nominal':>9}{'sender':>9}{'recv':>9}{'loss %':>9}"
              f"{'p50 ms':>9}{'p95 ms':>9}{'max ms':>9}{'jit ms':>8}{'stall':>6}{'integ':>6}  ok")
        for x in s["streams"]:
            print(f"{x['name']:<12}{_f(x['nominal_hz']):>9}{_f(x.get('sender_hz')):>9}"
                  f"{_f(x.get('recv_hz')):>9}{_f(x.get('loss_pct')):>9}"
                  f"{_f(x.get('latency_ms_p50')):>9}{_f(x.get('latency_ms_p95')):>9}"
                  f"{_f(x.get('latency_ms_max')):>9}{_f(x.get('interarrival_ms_std')):>8}"
                  f"{x.get('stalls', ''):>6}{(x.get('crc_errors') or 0) + (x.get('pattern_errors') or 0):>6}"
                  f"  {'n/a' if x.get('passed') is None else 'yes' if x['passed'] else 'NO'}")
    if s.get("bulk"):
        b = s["bulk"]
        print(f"bulk: {b['received_bytes']}/{b['expected_bytes']} B in {b['seconds']} s = "
              f"{b['MBps']} MB/s, sha256 {'OK' if b.get('sha256_ok') else 'MISMATCH'}")
    for r in s.get("ramp") or []:
        print(f"ramp step {r['step']}: camera {r['camera_fps']} fps x {r['frame_bytes']} B -> "
              f"{r['throughput_kBps']} kB/s, loss {r['loss_pct']} %, p95 {r['latency_ms_p95']} ms "
              f"{'ok' if r['passed'] else 'FAIL'}")
    if s.get("max_sustainable_kBps"):
        print(f"max sustainable throughput: {s['max_sustainable_kBps']} kB/s"
              + ("  (ramp ceiling reached - the link may carry more)" if s.get("ramp_ceiling_reached") else ""))
    print(f"saved to {RUNS_DIR / s['run_id']}")


def _f(v) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def cli() -> None:
    p = argparse.ArgumentParser(prog="python -m linkbench.host")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one benchmark headless")
    r.add_argument("--transport", default="ws", choices=["ws", "udp", "rfcomm", "ble"])
    r.add_argument("--addr", default="127.0.0.1", help="Pi IP, Bluetooth MAC, or BLE name/MAC")
    r.add_argument("--port", type=int, help="ws/udp port or RFCOMM channel")
    r.add_argument("--link", default="local", choices=["usb", "ap", "lan", "bt", "ble", "local"])
    r.add_argument("--profile", default="full", choices=list(presets()))
    r.add_argument("--duration", type=float)
    r.add_argument("--source", default="synthetic", choices=["synthetic", "real"])
    r.add_argument("--bulk-mb", type=float)
    r.add_argument("--drop", type=float, default=0, help="inject loss %% (testing)")
    r.add_argument("--delay", type=float, default=0, help="inject delay ms (testing)")
    r.add_argument("--jitter", type=float, default=0, help="inject jitter ms (testing)")
    r.add_argument("--corrupt", type=float, default=0, help="inject corruption %% (testing)")
    r.add_argument("--note", default="")

    s = sub.add_parser("serve", help="start the dashboard")
    s.add_argument("--port", type=int, default=8770)
    s.add_argument("--host", default="127.0.0.1")

    sub.add_parser("list", help="list recorded runs")
    rep = sub.add_parser("report", help="write a self-contained HTML report for a run")
    rep.add_argument("run_id")

    args = p.parse_args()
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "run":
        cfg = RunConfig(transport=args.transport, addr=args.addr, port=args.port, link=args.link,
                        profile=args.profile, duration_s=args.duration, source=args.source,
                        bulk_bytes=int(args.bulk_mb * 1024 * 1024) if args.bulk_mb else None,
                        faults={"drop_pct": args.drop, "delay_ms": args.delay,
                                "jitter_ms": args.jitter, "corrupt_pct": args.corrupt},
                        note=args.note)
        summary = asyncio.run(BenchSession(cfg).run())
        _print_summary(summary)
        sys.exit(0 if summary["passed"] else 1)
    elif args.cmd == "serve":
        import uvicorn
        from .app import app
        print(f"dashboard: http://{args.host}:{args.port}")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    elif args.cmd == "list":
        for s in list_runs():
            print(f"{s['run_id']:<55} {'PASS' if s['passed'] else 'FAIL'}  {s['profile_label']}")
    elif args.cmd == "report":
        from .report import write_report
        print(write_report(args.run_id))


if __name__ == "__main__":
    cli()
