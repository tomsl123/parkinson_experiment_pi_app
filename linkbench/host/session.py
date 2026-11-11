"""One benchmark run, host side: connect, sync clocks, drive the agent, measure, record.

Flow: connect → hello → 1 s of pings for the clock → streams / ramp / bulk → stop →
drain → summary written to runs/<run_id>/. A live snapshot is pushed every 200 ms to an
optional callback (the dashboard).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

from ..common import transports
from ..common.clocksync import ClockSync, PingSample
from ..common.packet import (STREAM_BULK, FrameError, control, decode, now_ns, pattern,
                             pattern_ok)
from ..common.profiles import DEFAULT_THRESHOLDS, StreamSpec, camera, presets
from ..common.stats import Rec, StreamStats, histogram, percentile
from ..common.transports.base import TransportClosed
from ..common.transports.faulty import FaultyTransport
from .recorder import RUNS_DIR, write_run

log = logging.getLogger("linkbench.host")

WINDOW_S = 5.0
UPDATE_S = 0.2


@dataclass
class RunConfig:
    transport: str = "ws"
    addr: str = "127.0.0.1"
    port: Optional[int] = None          # ws/udp port or rfcomm channel
    link: str = "local"                 # usb | ap | lan | bt | ble | local
    profile: str = "full"
    duration_s: Optional[float] = None  # None: profile default
    source: str = "synthetic"           # synthetic | real
    streams: Optional[list] = None      # list of StreamSpec dicts, overrides the profile
    bulk_bytes: Optional[int] = None
    bulk_chunk: int = 16384
    ping_hz: float = 10.0
    thresholds: dict = field(default_factory=dict)
    faults: dict = field(default_factory=dict)   # drop_pct, delay_ms, jitter_ms, corrupt_pct
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "RunConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class BenchSession:
    def __init__(self, cfg: RunConfig,
                 on_update: Optional[Callable[[dict], Awaitable[None]]] = None,
                 runs_dir=RUNS_DIR) -> None:
        self.cfg = cfg
        self.on_update = on_update
        self.runs_dir = runs_dir
        self.profile = presets()[cfg.profile]
        self.thresholds = {**DEFAULT_THRESHOLDS, **cfg.thresholds}
        self.run_id = "{}_{}_{}_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S"),
                                           cfg.link, cfg.transport, cfg.profile)
        self.streams: dict[int, StreamStats] = {}
        self.clock = ClockSync()
        self.pending: dict[int, int] = {}
        self.ping_id = 0
        self.ctl_seq = 0
        self.step = 0
        self.ramp_results: list = []
        self.bulk: Optional[dict] = None
        self.agent: dict = {}
        self.sources: dict = {}
        self.agent_stats: dict = {}
        self.sysinfo_log: list = []
        self.sysinfo: dict = {}
        self.malformed = 0
        self.state = "idle"
        self.error: Optional[str] = None
        self.t = None
        self.t0_ns = now_ns()
        self.started_wall = datetime.now().isoformat(timespec="seconds")
        self._stop = asyncio.Event()
        self._ev = {k: asyncio.Event() for k in ("hello", "started", "stopped", "bulk")}
        self._cursor: dict = {}
        self._ping_cursor = 0
        self._tick = 0
        self.summary: Optional[dict] = None

    # ── public ─────────────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> dict:
        tasks = []
        try:
            self.state = "connecting"
            self.t = await self._connect()
            tasks.append(asyncio.create_task(self._receiver()))
            await self._handshake()
            pinger = asyncio.create_task(self._pinger())
            tasks.append(pinger)
            if self.on_update:
                tasks.append(asyncio.create_task(self._updater()))
            self.state = "syncing"
            await self._sleep(1.0)
            self.state = "running"
            self.t0_ns = now_ns()
            if self.profile.ramp and not self.cfg.streams:
                await self._run_ramp()
            elif self.cfg.bulk_bytes or self.profile.bulk_bytes:
                await self._run_bulk()
            else:
                await self._run_streams()
            pinger.cancel()             # nothing may follow the bye
            await self._send({"type": "bye"})
        except (TransportClosed, OSError, RuntimeError, ValueError) as e:
            self.error = self.error or str(e)
            log.error("run failed: %s", e)
        finally:
            self.state = "stopping"
            await asyncio.sleep(0.3)
            for task in tasks:
                task.cancel()
            if self.t is not None:
                try:
                    await self.t.close()
                except Exception:
                    pass
            self.state = "done"
            self.summary = self.summarize()
            write_run(self.run_id, self.summary, self.streams, self.clock.samples,
                      self.sysinfo_log, self.runs_dir)
            if self.on_update:
                await self.on_update({"t": "done", "summary": self.summary})
        return self.summary

    # ── connection ─────────────────────────────────────────────────────────────

    async def _connect(self):
        c = self.cfg
        opts = {"addr": c.addr}
        if c.transport in ("ws", "udp") and c.port:
            opts["port"] = c.port
        if c.transport == "rfcomm":
            opts["channel"] = c.port or 1
        t = await transports.connect(c.transport, **opts)
        if any(c.faults.get(k) for k in ("drop_pct", "delay_ms", "jitter_ms", "corrupt_pct")):
            t = FaultyTransport(t, **{k: float(v) for k, v in c.faults.items() if v})
        return t

    async def _send(self, msg: dict) -> None:
        self.ctl_seq += 1
        await self.t.send(control(msg, self.ctl_seq))

    async def _request(self, msg: dict, event: str, timeout: float, tries: int) -> bool:
        """Send a control message and wait for its answer; retries cover UDP loss."""
        ev = self._ev[event]
        ev.clear()
        for _ in range(tries):
            await self._send(msg)
            try:
                await asyncio.wait_for(ev.wait(), timeout)
                return True
            except asyncio.TimeoutError:
                continue
        return False

    async def _handshake(self) -> None:
        mtu = getattr(getattr(self.t, "client", None), "mtu_size", None)
        if not await self._request({"type": "hello", "mtu": mtu}, "hello", 2.0, 5):
            raise RuntimeError("agent did not answer the hello – is it running and reachable?")

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    # ── receiving ──────────────────────────────────────────────────────────────

    async def _receiver(self) -> None:
        try:
            while True:
                data = await self.t.recv()
                t_recv = now_ns()
                try:
                    f = decode(data)
                except FrameError:
                    self.malformed += 1
                    continue
                if f.is_control:
                    if f.crc_ok:
                        self._on_control(f.json(), t_recv)
                    else:
                        self.malformed += 1
                elif f.stream == STREAM_BULK:
                    self._on_bulk(f, t_recv)
                else:
                    st = self.streams.get(f.stream)
                    if st is None:
                        if not f.crc_ok:
                            self.malformed += 1
                            continue
                        st = self.streams[f.stream] = StreamStats(f.stream, f"stream{f.stream}")
                    st.add(Rec(f.seq, f.t_send_ns, t_recv, f.size, f.crc_ok, pattern_ok(f),
                               self.step))
        except TransportClosed as e:
            if self.state in ("syncing", "running"):
                self.error = f"connection lost after {(now_ns() - self.t0_ns) / 1e9:.1f} s: {e}"
                self._stop.set()

    def _on_control(self, msg: dict, t_recv: int) -> None:
        kind = msg.get("type")
        if kind == "pong":
            t0 = self.pending.pop(msg["id"], None)
            if t0 is not None:
                self.clock.add(PingSample(t0, msg["t1"], msg["t2"], t_recv))
        elif kind == "hello_ack":
            self.agent = msg
            self._ev["hello"].set()
        elif kind == "started":
            self.sources = msg.get("sources", {})
            self._ev["started"].set()
        elif kind == "stopped":
            self.agent_stats = msg.get("agent_stats", {})
            self._ev["stopped"].set()
        elif kind == "sysinfo":
            msg["t_s"] = round((t_recv - self.t0_ns) / 1e9, 2)
            self.sysinfo = msg
            self.sysinfo_log.append(msg)
            self.agent_stats = msg.get("agent_stats") or self.agent_stats
        elif kind == "bulk_done":
            if self.bulk is not None:
                self.bulk["agent"] = msg
            self._ev["bulk"].set()
        elif kind == "error":
            self.error = msg.get("message")
            self._stop.set()

    def _on_bulk(self, f, t_recv: int) -> None:
        b = self.bulk
        if b is None:
            return
        b["t_first"] = b["t_first"] or t_recv
        b["t_last"] = t_recv
        if f.seq not in b["chunks"]:
            b["chunks"][f.seq] = f.payload
            b["bytes"] += len(f.payload)
        if not f.crc_ok or pattern_ok(f) is False:
            b["integrity_errors"] += 1

    async def _pinger(self) -> None:
        period = 1.0 / self.cfg.ping_hz
        try:
            while True:
                self.ping_id += 1
                t0 = now_ns()
                self.pending[self.ping_id] = t0
                self.clock.sent += 1
                await self._send({"type": "ping", "id": self.ping_id, "t0": t0})
                stale = [k for k, v in self.pending.items() if t0 - v > 5e9]
                for k in stale:
                    del self.pending[k]
                await asyncio.sleep(period)
        except TransportClosed:
            pass

    # ── modes ──────────────────────────────────────────────────────────────────

    def _specs(self) -> list:
        if self.cfg.streams:
            return [StreamSpec.from_dict(d) for d in self.cfg.streams]
        return list(self.profile.streams)

    async def _start(self, specs: list, keep_seq: bool = False) -> None:
        for s in specs:
            st = self.streams.get(s.stream)
            if st is None:
                self.streams[s.stream] = StreamStats(s.stream, s.name, s)
            else:
                st.spec, st.name = s, s.name
        ok = await self._request({"type": "start", "streams": [s.to_dict() for s in specs],
                                  "source": self.cfg.source, "keep_seq": keep_seq},
                                 "started", 5.0, 3)
        if not ok:
            raise RuntimeError("agent did not confirm the stream start")

    async def _stop_streams(self) -> None:
        if await self._request({"type": "stop"}, "stopped", 3.0, 3):
            for st in self.streams.values():
                a = self.agent_stats.get(st.name)
                if a:
                    st.last_sent_seq = a.get("last_seq")
        await asyncio.sleep(0.5)        # let in-flight frames arrive

    async def _run_streams(self) -> None:
        specs = self._specs()
        duration = self.cfg.duration_s or self.profile.duration_s or 30.0
        if specs:
            await self._start(specs)
        await self._sleep(duration)
        if specs:
            await self._stop_streams()

    async def _run_ramp(self) -> None:
        ramp = self.profile.ramp
        base = list(self.profile.streams)
        for i, fps in enumerate(ramp.fps_steps):
            if self._stop.is_set():
                break
            self.step = i
            cam = camera(float(fps), ramp.frame_bytes)
            await self._start(base + [cam], keep_seq=True)
            await self._sleep(ramp.step_s)
            res = self._ramp_step(i, cam)
            self.ramp_results.append(res)
            if not res["passed"]:
                break
        await self._stop_streams()

    def _ramp_step(self, i: int, cam: StreamSpec) -> dict:
        st = self.streams[cam.stream]
        s = StreamStats(cam.stream, cam.name, cam, st.recs).summary(self.clock.offset_at, step=i)
        total_bytes = sum(r.size for x in self.streams.values() for r in x.recs if r.step == i)
        recs = [r for x in self.streams.values() for r in x.recs if r.step == i]
        span = (max(r.t_recv for r in recs) - min(r.t_recv for r in recs)) / 1e9 if recs else 0
        ramp = self.profile.ramp
        loss = s.get("loss_pct") or 0.0
        p95 = s.get("latency_ms_p95")
        ok = (s.get("received", 0) > 0 and loss <= ramp.max_loss_pct
              and (p95 is None or p95 <= ramp.max_p95_ms)
              and not (s.get("crc_errors") or s.get("pattern_errors")))
        return {"step": i, "camera_fps": cam.rate_hz, "frame_bytes": cam.sample_bytes,
                "offered_kBps": round(cam.rate_hz * cam.sample_bytes / 1000, 1),
                "throughput_kBps": round(total_bytes / span / 1000, 1) if span else None,
                "camera_recv_hz": s.get("recv_hz"), "loss_pct": loss,
                "latency_ms_p95": p95, "passed": ok}

    async def _run_bulk(self) -> None:
        n = int(self.cfg.bulk_bytes or self.profile.bulk_bytes)
        self.bulk = {"expected": n, "bytes": 0, "chunks": {}, "t_first": None, "t_last": None,
                     "integrity_errors": 0, "agent": None}
        await self._send({"type": "bulk", "bytes": n, "chunk": self.cfg.bulk_chunk})
        timeout = max(60.0, n / 20_000)       # assume at least 20 kB/s
        deadline = time.monotonic() + timeout
        grace_until = None
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.bulk["bytes"] >= n:
                break
            if self._ev["bulk"].is_set():
                # the agent has sent everything; on lossy links some chunks never arrive
                grace_until = grace_until or time.monotonic() + 2.0
                if time.monotonic() > grace_until:
                    break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)

    # ── live snapshot ──────────────────────────────────────────────────────────

    async def _updater(self) -> None:
        while True:
            await asyncio.sleep(UPDATE_S)
            try:
                await self.on_update(self.snapshot())
            except Exception as e:      # never let the UI kill the run
                log.debug("update failed: %s", e)

    def snapshot(self) -> dict:
        now = now_ns()
        self._tick += 1
        since = now - int(WINDOW_S * 1e9)
        off = self.clock.offset_at
        window, series, thr, hist = [], {}, {}, {}
        for st in sorted(self.streams.values(), key=lambda s: s.stream):
            window.append(st.summary(off, since_ns=since, thresholds=self.thresholds))
            cur = self._cursor.get(st.stream, 0)
            new = st.recs[cur:]
            self._cursor[st.stream] = len(st.recs)
            stride = max(1, len(new) // 60)
            pts = []
            for r in new[::stride]:
                o = off(r.t_recv)
                if o is not None:
                    pts.append([round((r.t_recv - self.t0_ns) / 1e9, 3),
                                round((r.t_recv - (r.t_send - o)) / 1e6, 3)])
            series[st.name] = pts
            last1 = [r for r in st.recs[-2000:] if r.t_recv >= now - 1_000_000_000]
            thr[st.name] = round(sum(r.size for r in last1) / 1000, 2)
            if st.spec:
                recent = [r.t_recv for r in st.recs[-2000:] if r.t_recv >= since]
                gaps = [(b - a) / 1e6 for a, b in zip(recent, recent[1:])]
                period = 1000.0 / st.spec.frame_rate
                hist[st.name] = histogram(gaps, 40, 0.0, 4 * period)
        total = None
        if self._tick % 5 == 0 or self.state != "running":
            total = [st.summary(off, thresholds=self.thresholds)
                     for st in sorted(self.streams.values(), key=lambda s: s.stream)]
        rtt_new = self.clock.samples[self._ping_cursor:]
        self._ping_cursor = len(self.clock.samples)
        return {
            "t": "update", "run_id": self.run_id, "state": self.state, "error": self.error,
            "elapsed_s": round((now - self.t0_ns) / 1e9, 2),
            "config": asdict(self.cfg), "agent": self.agent, "sources": self.sources,
            "sysinfo": self.sysinfo, "agent_stats": self.agent_stats,
            "clock": self._clock_summary(window_only=True),
            "streams_window": window, "streams_total": total,
            "series": {"latency": series, "throughput": thr,
                       "rtt": [[round((s.t3 - self.t0_ns) / 1e9, 3), round(s.rtt_ns / 1e6, 3)]
                               for s in rtt_new]},
            "hist": hist, "bulk": self._bulk_summary(), "ramp": self.ramp_results,
            "malformed": self.malformed,
        }

    # ── summaries ──────────────────────────────────────────────────────────────

    def _clock_summary(self, window_only: bool = False) -> dict:
        samples = self.clock.samples[-50:] if window_only else self.clock.samples
        rtts = sorted(s.rtt_ns / 1e6 for s in samples)
        off = self.clock.offset_now()
        answered = len(self.clock.samples)
        sent = max(self.clock.sent - len(self.pending), answered)
        return {
            "offset_ms": round(off / 1e6, 3) if off is not None else None,
            "drift_ppm": round(self.clock.drift_ppm(), 2) if self.clock.drift_ppm() is not None else None,
            "rtt_ms_last": round(samples[-1].rtt_ns / 1e6, 3) if samples else None,
            "rtt_ms_min": round(rtts[0], 3) if rtts else None,
            "rtt_ms_p50": _r(percentile(rtts, 50)), "rtt_ms_p95": _r(percentile(rtts, 95)),
            "rtt_ms_max": round(rtts[-1], 3) if rtts else None,
            "pings_sent": sent, "pings_answered": answered,
            "ping_loss_pct": round(100 * (sent - answered) / sent, 2) if sent else None,
        }

    def _bulk_summary(self) -> Optional[dict]:
        b = self.bulk
        if b is None:
            return None
        span = ((b["t_last"] - b["t_first"]) / 1e9) if b["t_first"] and b["t_last"] else None
        out = {"expected_bytes": b["expected"], "received_bytes": b["bytes"],
               "progress_pct": round(100 * b["bytes"] / b["expected"], 1) if b["expected"] else None,
               "chunks": len(b["chunks"]), "integrity_errors": b["integrity_errors"],
               "seconds": round(span, 3) if span else None,
               "MBps": round(b["bytes"] / span / 1e6, 3) if span else None}
        agent = b.get("agent")
        if agent:
            out["chunks_sent"] = agent["chunks"]
            out["missing_chunks"] = agent["chunks"] - len(b["chunks"])
            if self.state in ("stopping", "done"):
                h = hashlib.sha256()
                for seq in sorted(b["chunks"]):
                    h.update(b["chunks"][seq])
                out["sha256_ok"] = h.hexdigest() == agent["sha256"]
        return out

    def summarize(self) -> dict:
        off = self.clock.offset_at
        streams = [st.summary(off, thresholds=self.thresholds, final=True)
                   for st in sorted(self.streams.values(), key=lambda s: s.stream)]
        if self.ramp_results:
            # the camera rate changes every step; it is judged per step in the ramp table
            for s in streams:
                if s["name"] == "camera":
                    s.update(passed=None, rate_error_pct=None, sender_error_pct=None,
                             note="rate varies per ramp step - see the ramp table")
        bulk = self._bulk_summary()
        passed = (self.error is None
                  and all(s.get("passed") is not False for s in streams)
                  and (bulk is None or bool(bulk.get("sha256_ok"))))
        max_ok = [r["throughput_kBps"] for r in self.ramp_results if r["passed"]]
        last_s = self.sysinfo_log[-1] if self.sysinfo_log else {}
        cpu = [s.get("cpu_pct") for s in self.sysinfo_log if s.get("cpu_pct") is not None]
        return {
            "run_id": self.run_id, "started_at": self.started_wall,
            "config": asdict(self.cfg), "profile_label": self.profile.label,
            "thresholds": self.thresholds,
            "transport": self.t.info() if self.t else None,
            "agent": self.agent, "sources": self.sources,
            "duration_s": round((now_ns() - self.t0_ns) / 1e9, 2),
            "error": self.error, "passed": passed,
            "streams": streams, "agent_stats": self.agent_stats,
            "clock": self._clock_summary(),
            "bulk": bulk,
            "ramp": self.ramp_results,
            "max_sustainable_kBps": max(max_ok) if max_ok else None,
            "ramp_ceiling_reached": bool(self.ramp_results) and all(r["passed"] for r in self.ramp_results)
                                    and len(self.ramp_results) == len(self.profile.ramp.fps_steps),
            "malformed_frames": self.malformed,
            "injected_faults": getattr(self.t, "injected", None),
            "pi": {"board": last_s.get("board") or self.agent.get("board"),
                   "cpu_pct_max": max(cpu) if cpu else None,
                   "temp_c_last": last_s.get("temp_c"), "throttled": last_s.get("throttled"),
                   "wifi": last_s.get("wifi")},
        }


def _r(v, nd=3):
    return None if v is None else round(v, nd)
