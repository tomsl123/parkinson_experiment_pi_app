"""Common transport interface.

Every transport moves whole frames (bytes) in both directions. The agent side calls
`serve(handler, **opts)` and gets one `Transport` per session; the host side calls
`connect(**opts)`.
"""
from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from typing import Optional

LEN = struct.Struct("<I")
MAX_FRAME = 16 * 1024 * 1024


class TransportClosed(Exception):
    pass


class Transport(ABC):
    name = "base"
    reliable = True          # in-order and lossless by design (TCP, RFCOMM)

    def __init__(self) -> None:
        self.peer: str = ""
        self.local: Optional[str] = None

    @abstractmethod
    async def send(self, data: bytes) -> None: ...

    @abstractmethod
    async def recv(self) -> bytes: ...

    @abstractmethod
    async def close(self) -> None: ...

    def configure(self, **kwargs) -> None:
        """Transport-specific tuning after the handshake (e.g. BLE MTU)."""

    def info(self) -> dict:
        return {"transport": self.name, "peer": self.peer, "local": self.local,
                "reliable": self.reliable}


class LengthPrefixReader:
    """Reassembles length-prefixed frames from a byte stream."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out = []
        while len(self._buf) >= LEN.size:
            (n,) = LEN.unpack_from(self._buf, 0)
            if n > MAX_FRAME:
                raise TransportClosed(f"frame length {n} exceeds limit; stream out of sync")
            if len(self._buf) < LEN.size + n:
                break
            out.append(bytes(self._buf[LEN.size:LEN.size + n]))
            del self._buf[:LEN.size + n]
        return out


def length_prefixed(data: bytes) -> bytes:
    return LEN.pack(len(data)) + data
