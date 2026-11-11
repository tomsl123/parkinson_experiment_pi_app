"""Fault injection on the receiving side, for testing the measurement itself.

Wraps any transport and applies, to data frames only (control frames pass untouched so
the session keeps working): random drops, a fixed delay plus random jitter (which can
reorder frames, like a real network), and random single-byte corruption.
"""
from __future__ import annotations

import asyncio
import random
from typing import Optional

from ..packet import HEADER_SIZE, STREAM_CONTROL
from .base import Transport, TransportClosed

STREAM_BYTE = 4   # offset of the stream id in the header


class FaultyTransport(Transport):
    def __init__(self, inner: Transport, drop_pct: float = 0.0, delay_ms: float = 0.0,
                 jitter_ms: float = 0.0, corrupt_pct: float = 0.0,
                 seed: Optional[int] = None) -> None:
        super().__init__()
        self.inner = inner
        self.name = inner.name
        self.reliable = inner.reliable
        self.peer, self.local = inner.peer, inner.local
        self.drop_pct, self.delay_ms = drop_pct, delay_ms
        self.jitter_ms, self.corrupt_pct = jitter_ms, corrupt_pct
        self.rng = random.Random(seed)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.injected = {"dropped": 0, "corrupted": 0, "delayed": 0}
        self._pump = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                data = await self.inner.recv()
                if len(data) > STREAM_BYTE and data[STREAM_BYTE] != STREAM_CONTROL:
                    if self.rng.random() * 100 < self.drop_pct:
                        self.injected["dropped"] += 1
                        continue
                    if self.rng.random() * 100 < self.corrupt_pct and len(data) > HEADER_SIZE + 4:
                        b = bytearray(data)
                        i = self.rng.randrange(HEADER_SIZE, len(b) - 4)
                        b[i] ^= 0xFF
                        data = bytes(b)
                        self.injected["corrupted"] += 1
                    delay = self.delay_ms + (self.rng.random() * self.jitter_ms if self.jitter_ms else 0)
                    if delay > 0:
                        self.injected["delayed"] += 1
                        asyncio.get_running_loop().call_later(delay / 1000.0,
                                                              self.queue.put_nowait, data)
                        continue
                self.queue.put_nowait(data)
        except TransportClosed:
            self.queue.put_nowait(None)

    async def send(self, data: bytes) -> None:
        await self.inner.send(data)

    async def recv(self) -> bytes:
        data = await self.queue.get()
        if data is None:
            raise TransportClosed("closed")
        return data

    async def close(self) -> None:
        self._pump.cancel()
        await self.inner.close()

    def configure(self, **kwargs) -> None:
        self.inner.configure(**kwargs)

    def info(self) -> dict:
        d = self.inner.info()
        d["faults"] = {"drop_pct": self.drop_pct, "delay_ms": self.delay_ms,
                       "jitter_ms": self.jitter_ms, "corrupt_pct": self.corrupt_pct}
        return d
