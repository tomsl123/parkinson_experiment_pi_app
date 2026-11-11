"""Binary frame format shared by agent and host.

Layout (little-endian):

    magic     3s   b"LB1"
    version   B
    stream    B    0 = control (JSON payload), 1..249 = data streams, 250 = bulk transfer
    flags     B    bit 0: payload is the deterministic test pattern
    seq       I    per-stream sequence number
    t_send    q    sender clock, time.perf_counter_ns() at the moment of sending
    length    I    payload length in bytes
    payload   ...
    crc32     I    over header + payload

Synthetic payloads are generated from (stream, seq), so the receiver can verify every
byte, not only the checksum.
"""
from __future__ import annotations

import json
import random
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Optional

MAGIC = b"LB1"
VERSION = 1

STREAM_CONTROL = 0
STREAM_BULK = 250

FLAG_PATTERN = 0x01

HEADER = struct.Struct("<3sBBBIqI")
CRC = struct.Struct("<I")
HEADER_SIZE = HEADER.size
OVERHEAD = HEADER.size + CRC.size


def now_ns() -> int:
    """Monotonic high-resolution clock used for every timestamp.

    perf_counter is monotonic on Linux (CLOCK_MONOTONIC) and on Windows (QPC), unlike
    time.monotonic, which ticks in ~15.6 ms steps on Windows.
    """
    return time.perf_counter_ns()


@dataclass
class Frame:
    stream: int
    seq: int
    t_send_ns: int
    payload: bytes
    flags: int = 0
    crc_ok: bool = True
    size: int = 0

    @property
    def is_control(self) -> bool:
        return self.stream == STREAM_CONTROL

    def json(self) -> dict:
        return json.loads(self.payload.decode())


class FrameError(ValueError):
    pass


def encode(stream: int, seq: int, payload: bytes, flags: int = 0,
           t_send_ns: Optional[int] = None) -> bytes:
    if t_send_ns is None:
        t_send_ns = now_ns()
    head = HEADER.pack(MAGIC, VERSION, stream, flags, seq & 0xFFFFFFFF, t_send_ns, len(payload))
    body = head + payload
    return body + CRC.pack(zlib.crc32(body) & 0xFFFFFFFF)


def decode(buf: bytes) -> Frame:
    if len(buf) < OVERHEAD:
        raise FrameError(f"frame too short ({len(buf)} bytes)")
    magic, version, stream, flags, seq, t_send, length = HEADER.unpack_from(buf, 0)
    if magic != MAGIC or version != VERSION:
        raise FrameError("bad magic/version")
    end = HEADER_SIZE + length
    if len(buf) != end + CRC.size:
        raise FrameError(f"length mismatch: header says {length}, frame has {len(buf) - OVERHEAD}")
    (crc,) = CRC.unpack_from(buf, end)
    crc_ok = (zlib.crc32(buf[:end]) & 0xFFFFFFFF) == crc
    return Frame(stream=stream, seq=seq, t_send_ns=t_send, payload=bytes(buf[HEADER_SIZE:end]),
                 flags=flags, crc_ok=crc_ok, size=len(buf))


def control(msg: dict, seq: int = 0) -> bytes:
    return encode(STREAM_CONTROL, seq, json.dumps(msg, separators=(",", ":")).encode())


def pattern(stream: int, seq: int, n: int) -> bytes:
    """Deterministic pseudo-random payload for (stream, seq)."""
    if n <= 0:
        return b""
    return random.Random((stream << 32) | (seq & 0xFFFFFFFF)).randbytes(n)


def pattern_ok(frame: Frame) -> Optional[bool]:
    """True/False for pattern payloads, None when the payload is real sensor data."""
    if not frame.flags & FLAG_PATTERN:
        return None
    return frame.payload == pattern(frame.stream, frame.seq, len(frame.payload))
