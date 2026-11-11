"""Bluetooth Classic (RFCOMM) – a serial-like byte stream, length-prefixed frames.

Uses Python's built-in AF_BLUETOOTH sockets (Linux and Windows; not available on
macOS). Blocking socket calls run in threads so the asyncio loop stays responsive.
The Pi must be paired with the computer first (see pi_setup/bt_pair.md).
"""
from __future__ import annotations

import asyncio
import socket
import threading

from .base import LengthPrefixReader, Transport, TransportClosed, length_prefixed

ANY_ADDR = "00:00:00:00:00:00"


def _require_bt() -> None:
    if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_RFCOMM"):
        raise TransportClosed("this Python has no Bluetooth socket support (macOS?)")


class RfcommTransport(Transport):
    name = "rfcomm"
    reliable = True

    def __init__(self, sock: socket.socket, peer: str) -> None:
        super().__init__()
        self.sock = sock
        self.peer = peer
        self.queue: asyncio.Queue = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        self.send_lock = asyncio.Lock()
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        reader = LengthPrefixReader()
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                for frame in reader.feed(data):
                    self.loop.call_soon_threadsafe(self.queue.put_nowait, frame)
        except (OSError, TransportClosed):
            pass
        self.loop.call_soon_threadsafe(self.queue.put_nowait, None)

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise TransportClosed("closed")
        async with self.send_lock:
            try:
                await asyncio.to_thread(self.sock.sendall, length_prefixed(data))
            except OSError as e:
                raise TransportClosed(str(e)) from e

    async def recv(self) -> bytes:
        data = await self.queue.get()
        if data is None:
            self.closed = True
            raise TransportClosed("RFCOMM connection closed")
        return data

    async def close(self) -> None:
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


async def serve(handler, channel: int = 1, **_) -> None:
    _require_bt()
    srv = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    srv.bind((ANY_ADDR, channel))
    srv.listen(1)
    while True:
        conn, addr = await asyncio.to_thread(srv.accept)
        t = RfcommTransport(conn, f"{addr[0]} ch{channel}")
        t.local = f"rfcomm ch{channel}"
        try:
            await handler(t)
        finally:
            await t.close()


async def connect(addr: str, channel: int = 1, **_) -> RfcommTransport:
    _require_bt()
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    try:
        await asyncio.wait_for(asyncio.to_thread(sock.connect, (addr, channel)), 20)
    except (OSError, asyncio.TimeoutError) as e:
        sock.close()
        raise TransportClosed(f"cannot connect RFCOMM {addr} ch{channel} ({e})") from e
    return RfcommTransport(sock, f"{addr} ch{channel}")
