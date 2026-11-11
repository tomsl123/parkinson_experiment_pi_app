"""UDP – one frame per datagram. Shows the link's real loss and reordering, which TCP hides.

The agent learns the host's address from the host's first datagram (the hello).
Frames above ~64 kB cannot be sent; larger camera frames would need splitting.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from .base import Transport, TransportClosed

MAX_DATAGRAM = 65000
IDLE_TIMEOUT_S = 15.0


def _is_hello(data: bytes) -> bool:
    # control frame (stream byte 0) carrying {"type":"hello"...}
    return len(data) > 4 and data[4] == 0 and b'"type":"hello"' in data


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, on_datagram) -> None:
        self.on_datagram = on_datagram
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data, addr) -> None:
        self.on_datagram(data, addr)

    def error_received(self, exc) -> None:
        # Windows reports ICMP port-unreachable here; keep the session alive
        pass


class UdpTransport(Transport):
    name = "udp"
    reliable = False

    def __init__(self, send_fn: Callable[[bytes], None], peer: tuple, local: Optional[tuple],
                 on_close: Optional[Callable[[], None]] = None) -> None:
        super().__init__()
        self._send_fn = send_fn
        self._on_close = on_close
        self.peer_addr = peer
        self.peer = "%s:%s" % peer[:2]
        self.local = "%s:%s" % local[:2] if local else None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def push(self, data: bytes) -> None:
        self.queue.put_nowait(data)

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise TransportClosed("closed")
        if len(data) > MAX_DATAGRAM:
            raise ValueError(f"frame of {len(data)} bytes is too large for one UDP datagram")
        self._send_fn(data)
        await asyncio.sleep(0)

    async def recv(self) -> bytes:
        if self.closed:
            raise TransportClosed("closed")
        try:
            data = await asyncio.wait_for(self.queue.get(), IDLE_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise TransportClosed(f"no datagram for {IDLE_TIMEOUT_S:.0f} s") from e
        if data is None:
            raise TransportClosed("closed")
        return data

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.queue.put_nowait(None)
        if self._on_close:
            self._on_close()


async def serve(handler, host: str = "0.0.0.0", port: int = 8766, **_) -> None:
    loop = asyncio.get_running_loop()
    state: dict = {"session": None}

    def on_datagram(data, addr):
        s = state["session"]
        if s is not None and not s.closed:
            if s.peer_addr == addr:
                s.push(data)
            return                      # one session at a time; ignore others
        if not _is_hello(data):
            return                      # stray datagram from an ended session
        dt = proto.transport
        s = UdpTransport(lambda d, a=addr: dt.sendto(d, a), addr, dt.get_extra_info("sockname"))
        state["session"] = s
        s.push(data)

        async def run():
            try:
                await handler(s)
            finally:
                await s.close()

        loop.create_task(run())

    proto = _Proto(on_datagram)
    await loop.create_datagram_endpoint(lambda: proto, local_addr=(host, port))
    await asyncio.Future()


async def connect(addr: str, port: int = 8766, **_) -> UdpTransport:
    loop = asyncio.get_running_loop()
    holder: dict = {}

    def on_datagram(data, _addr):
        t = holder.get("t")
        if t is not None:
            t.push(data)

    try:
        dt, _ = await loop.create_datagram_endpoint(lambda: _Proto(on_datagram),
                                                    remote_addr=(addr, port))
    except OSError as e:
        raise TransportClosed(f"cannot open UDP to {addr}:{port} ({e})") from e
    t = UdpTransport(dt.sendto, (addr, port), dt.get_extra_info("sockname"), on_close=dt.close)
    holder["t"] = t
    return t
