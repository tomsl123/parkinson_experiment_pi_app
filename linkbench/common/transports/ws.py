"""WebSocket over TCP – the transport the real app would most likely use.

Compression and keep-alive pings are disabled so the measurement sees the raw link;
linkbench does its own pings.
"""
from __future__ import annotations

import asyncio

import websockets

from .base import Transport, TransportClosed


class WsTransport(Transport):
    name = "ws"
    reliable = True

    def __init__(self, ws) -> None:
        super().__init__()
        self.ws = ws
        try:
            self.peer = "%s:%s" % tuple(ws.remote_address[:2])
            self.local = "%s:%s" % tuple(ws.local_address[:2])
        except Exception:
            pass

    async def send(self, data: bytes) -> None:
        try:
            await self.ws.send(data)
        except websockets.ConnectionClosed as e:
            raise TransportClosed(str(e)) from e

    async def recv(self) -> bytes:
        try:
            msg = await self.ws.recv()
        except websockets.ConnectionClosed as e:
            raise TransportClosed(str(e)) from e
        return msg.encode() if isinstance(msg, str) else msg

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass


_OPTS = dict(max_size=None, compression=None, ping_interval=None, ping_timeout=None)


async def serve(handler, host: str = "0.0.0.0", port: int = 8765, **_) -> None:
    async def _session(ws, *_args):
        await handler(WsTransport(ws))

    async with websockets.serve(_session, host, port, **_OPTS):
        await asyncio.Future()


async def connect(addr: str, port: int = 8765, **_) -> WsTransport:
    try:
        ws = await asyncio.wait_for(websockets.connect(f"ws://{addr}:{port}/", **_OPTS), 10)
    except (OSError, asyncio.TimeoutError) as e:
        raise TransportClosed(f"cannot connect to ws://{addr}:{port}/ ({e})") from e
    return WsTransport(ws)
