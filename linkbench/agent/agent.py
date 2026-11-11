"""Agent session: answers pings, runs test streams, sends bulk data and system info.

Control messages (JSON in stream-0 frames):

  host → agent   hello{mtu?} · ping{id,t0} · start{streams,source,keep_seq?}
                 · stop · bulk{bytes,chunk} · bye
  agent → host   hello_ack{board,transport,iface,version} · pong{id,t0,t1,t2}
                 · started{sources} · stopped{last_seq,agent_stats} · sysinfo{...}
                 · bulk_done{bytes,chunks,sha256,seconds} · error{message}

Streams are paced on absolute deadlines (`next += period`), never `sleep(period)`, so
processing time cannot slowly lower the rate. A stream that falls more than 5 periods
behind resets its schedule and counts an overrun; time spent waiting in `send()`
(transport backpressure) is measured separately.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional

from ..common.packet import (FLAG_PATTERN, STREAM_BULK, FrameError, control, decode,
                             encode, now_ns, pattern)
from ..common.profiles import StreamSpec
from ..common.transports.base import Transport, TransportClosed
from . import sysinfo as si
from .sources import SYNTHETIC, real_sensors

log = logging.getLogger("linkbench.agent")

AGENT_VERSION = "1.0"


@dataclass
class StreamState:
    spec: StreamSpec
    seq: int = 0
    sent: int = 0
    overruns: int = 0
    send_wait_ns: int = 0
    gen_ns: int = 0
    task: Optional[asyncio.Task] = None

    def stats(self) -> dict:
        return {"sent": self.sent, "last_seq": self.seq - 1, "overruns": self.overruns,
                "send_wait_ms": round(self.send_wait_ns / 1e6, 1),
                "gen_ms": round(self.gen_ns / 1e6, 1)}


class AgentSession:
    def __init__(self, transport: Transport, sysinfo: si.SysInfo) -> None:
        self.t = transport
        self.sysinfo = sysinfo
        self.streams: dict[int, StreamState] = {}
        self.tasks: set[asyncio.Task] = set()
        self.ctl_seq = 0

    async def send_ctl(self, msg: dict) -> None:
        self.ctl_seq += 1
        await self.t.send(control(msg, self.ctl_seq))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def run(self) -> None:
        log.info("session from %s via %s", self.t.peer, self.t.name)
        try:
            while True:
                data = await self.t.recv()
                t1 = now_ns()
                try:
                    frame = decode(data)
                except FrameError:
                    continue
                if not frame.is_control or not frame.crc_ok:
                    continue
                msg = json.loads(frame.payload)
                if await self.handle(msg, t1) is False:
                    break
        except TransportClosed as e:
            log.info("session ended: %s", e)
        finally:
            self.stop_streams()
            for task in list(self.tasks):
                task.cancel()
            await self.t.close()

    async def handle(self, msg: dict, t1: int):
        kind = msg.get("type")
        if kind == "ping":
            await self.send_ctl({"type": "pong", "id": msg["id"], "t0": msg["t0"],
                                 "t1": t1, "t2": now_ns()})
        elif kind == "hello":
            if msg.get("mtu"):
                self.t.configure(mtu=msg["mtu"])
            info = self.t.info()
            await self.send_ctl({"type": "hello_ack", "version": AGENT_VERSION,
                                 "board": self.sysinfo.model, "transport": info,
                                 "iface": si.interface_for(info.get("local"))})
            if not any(t.get_name() == "sysinfo" for t in self.tasks):
                self._spawn(self.sysinfo_loop()).set_name("sysinfo")
        elif kind == "start":
            await self.start_streams(msg)
        elif kind == "stop":
            self.stop_streams()
            await self.send_ctl({"type": "stopped",
                                 "agent_stats": {s.spec.name: s.stats()
                                                 for s in self.streams.values()}})
        elif kind == "bulk":
            self._spawn(self.bulk(int(msg["bytes"]), int(msg.get("chunk", 16384))))
        elif kind == "bye":
            return False
        return True

    # ── Streams ────────────────────────────────────────────────────────────────

    async def start_streams(self, msg: dict) -> None:
        keep_seq = bool(msg.get("keep_seq"))
        specs = [StreamSpec.from_dict(d) for d in msg.get("streams", [])]
        old = {k: v.seq for k, v in self.streams.items()} if keep_seq else {}
        self.stop_streams()
        self.streams = {}
        use_real = msg.get("source") == "real"
        sources = {}
        for spec in specs:
            src = real_sensors().source_for(spec) if use_real else SYNTHETIC
            st = StreamState(spec, seq=old.get(spec.stream, 0))
            self.streams[spec.stream] = st
            sources[spec.name] = (real_sensors().describe(spec) if use_real else "synthetic")
            st.task = self._spawn(self.stream_loop(st, src))
        await self.send_ctl({"type": "started", "sources": sources})

    def stop_streams(self) -> None:
        for st in self.streams.values():
            if st.task:
                st.task.cancel()
                st.task = None

    async def stream_loop(self, st: StreamState, src) -> None:
        spec = st.spec
        period = int(1e9 / spec.frame_rate)
        nxt = now_ns()
        try:
            while True:
                g0 = now_ns()
                payload = await src.payload(spec, st.seq)
                g1 = now_ns()
                await self.t.send(encode(spec.stream, st.seq, payload, src.flags))
                st.gen_ns += g1 - g0
                st.send_wait_ns += now_ns() - g1
                st.sent += 1
                st.seq += 1
                nxt += period
                delay = nxt - now_ns()
                if delay > 0:
                    await asyncio.sleep(delay / 1e9)
                else:
                    if -delay > 5 * period:
                        st.overruns += 1
                        nxt = now_ns()
                    await asyncio.sleep(0)
        except TransportClosed:
            pass

    # ── Bulk transfer ──────────────────────────────────────────────────────────

    async def bulk(self, total: int, chunk: int) -> None:
        h = hashlib.sha256()
        sent, seq = 0, 0
        t0 = now_ns()
        try:
            while sent < total:
                n = min(chunk, total - sent)
                data = pattern(STREAM_BULK, seq, n)
                h.update(data)
                await self.t.send(encode(STREAM_BULK, seq, data, FLAG_PATTERN))
                sent += n
                seq += 1
            await self.send_ctl({"type": "bulk_done", "bytes": sent, "chunks": seq,
                                 "sha256": h.hexdigest(), "seconds": (now_ns() - t0) / 1e9})
        except TransportClosed:
            pass

    # ── System info ────────────────────────────────────────────────────────────

    async def sysinfo_loop(self) -> None:
        try:
            while True:
                snap = await asyncio.to_thread(self.sysinfo.snapshot)
                snap["type"] = "sysinfo"
                snap["agent_stats"] = {s.spec.name: s.stats() for s in self.streams.values()}
                await self.send_ctl(snap)
                await asyncio.sleep(1.0)
        except TransportClosed:
            pass


class Agent:
    """Serves several transports at once; only one session runs at a time."""

    def __init__(self) -> None:
        self.sysinfo = si.SysInfo()
        self.busy = asyncio.Lock()

    async def handle(self, transport: Transport) -> None:
        if self.busy.locked():
            try:
                await transport.send(control({"type": "error",
                                              "message": "agent busy with another session"}))
            finally:
                await transport.close()
            return
        async with self.busy:
            await AgentSession(transport, self.sysinfo).run()
