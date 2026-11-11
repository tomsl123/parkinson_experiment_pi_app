"""Bluetooth Low Energy (GATT) – notifications agent→host, writes host→agent.

Frames are split into fragments that fit the negotiated ATT MTU:

    frag header  <HBB  message id, fragment index, fragment count
    chunk        up to (MTU - 3 - 4) bytes

Agent side uses `bless` (BlueZ D-Bus GATT server), host side uses `bleak`. Both are
optional imports, so the rest of linkbench works without them.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Optional

from .base import Transport, TransportClosed

SERVICE_UUID = "6f1d0000-4c42-4c42-8000-00805f9b34fb"
TX_UUID = "6f1d0001-4c42-4c42-8000-00805f9b34fb"      # agent → host (notify)
RX_UUID = "6f1d0002-4c42-4c42-8000-00805f9b34fb"      # host → agent (write)
DEVICE_NAME = "linkbench"

FRAG = struct.Struct("<HBB")
DEFAULT_CHUNK = 180       # safe for MTU >= 187; raised after the hello reports the host MTU
IDLE_TIMEOUT_S = 15.0


def fragment(msg_id: int, data: bytes, chunk: int) -> list[bytes]:
    parts = [data[i:i + chunk] for i in range(0, len(data), chunk)] or [b""]
    if len(parts) > 255:
        raise ValueError(f"frame of {len(data)} bytes needs {len(parts)} BLE fragments (max 255)")
    return [FRAG.pack(msg_id & 0xFFFF, i, len(parts)) + p for i, p in enumerate(parts)]


class Reassembler:
    def __init__(self) -> None:
        self.cur_id: Optional[int] = None
        self.parts: dict[int, bytes] = {}
        self.count = 0
        self.dropped = 0

    def feed(self, data: bytes) -> Optional[bytes]:
        if len(data) < FRAG.size:
            return None
        msg_id, idx, count = FRAG.unpack_from(data, 0)
        if msg_id != self.cur_id:
            if self.cur_id is not None and len(self.parts) < self.count:
                self.dropped += 1       # previous message incomplete
            self.cur_id, self.parts, self.count = msg_id, {}, count
        self.parts[idx] = data[FRAG.size:]
        if len(self.parts) == self.count:
            out = b"".join(self.parts[i] for i in range(self.count))
            self.cur_id, self.parts = None, {}
            return out
        return None


class _BleBase(Transport):
    name = "ble"
    reliable = True     # notifications are acknowledged at link layer, but may stall

    def __init__(self) -> None:
        super().__init__()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.reasm = Reassembler()
        self.chunk = DEFAULT_CHUNK
        self.msg_id = 0
        self.closed = False
        self.send_lock = asyncio.Lock()   # fragments of one frame must not interleave

    def on_fragment(self, data: bytes) -> None:
        frame = self.reasm.feed(bytes(data))
        if frame is not None:
            self.queue.put_nowait(frame)

    async def recv(self) -> bytes:
        try:
            data = await asyncio.wait_for(self.queue.get(), IDLE_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise TransportClosed(f"no BLE data for {IDLE_TIMEOUT_S:.0f} s") from e
        if data is None:
            raise TransportClosed("BLE disconnected")
        return data

    def configure(self, mtu: Optional[int] = None, **_) -> None:
        if mtu:
            self.chunk = max(20, int(mtu) - 3 - FRAG.size)

    def next_fragments(self, data: bytes) -> list[bytes]:
        self.msg_id = (self.msg_id + 1) & 0xFFFF
        return fragment(self.msg_id, data, self.chunk)


# ── Agent side (bless) ──────────────────────────────────────────────────────────

class BleServerTransport(_BleBase):
    def __init__(self, server) -> None:
        super().__init__()
        self.server = server
        self.peer = "BLE central"
        self.local = f"BLE peripheral '{DEVICE_NAME}'"

    async def send(self, data: bytes) -> None:
        if self.closed:
            raise TransportClosed("closed")
        char = self.server.get_characteristic(TX_UUID)
        async with self.send_lock:
            for frag in self.next_fragments(data):
                char.value = bytearray(frag)
                self.server.update_value(SERVICE_UUID, TX_UUID)
                await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


async def serve(handler, **_) -> None:
    try:
        from bless import (BlessServer, GATTAttributePermissions,
                           GATTCharacteristicProperties)
    except ImportError as e:
        raise TransportClosed("BLE agent needs `pip install bless`") from e

    loop = asyncio.get_running_loop()
    server = BlessServer(name=DEVICE_NAME, loop=loop)
    state: dict = {"session": None}

    def on_write(characteristic, value, **_kw):
        s = state["session"]
        if s is None or s.closed:
            s = BleServerTransport(server)
            state["session"] = s

            async def run():
                try:
                    await handler(s)
                finally:
                    await s.close()

            loop.create_task(run())
        s.on_fragment(value)

    server.read_request_func = lambda characteristic, **_kw: characteristic.value
    server.write_request_func = on_write
    await server.add_new_service(SERVICE_UUID)
    await server.add_new_characteristic(
        SERVICE_UUID, TX_UUID,
        GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read,
        None, GATTAttributePermissions.readable)
    await server.add_new_characteristic(
        SERVICE_UUID, RX_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response,
        None, GATTAttributePermissions.readable | GATTAttributePermissions.writeable)
    await server.start()
    await asyncio.Future()


# ── Host side (bleak) ───────────────────────────────────────────────────────────

class BleClientTransport(_BleBase):
    def __init__(self, client) -> None:
        super().__init__()
        self.client = client
        self.peer = str(getattr(client, "address", "BLE"))
        self.local = "BLE central"
        mtu = getattr(client, "mtu_size", None)
        self.configure(mtu=mtu)

    async def send(self, data: bytes) -> None:
        if self.closed or not self.client.is_connected:
            raise TransportClosed("BLE disconnected")
        async with self.send_lock:
            for frag in self.next_fragments(data):
                await self.client.write_gatt_char(RX_UUID, frag, response=False)

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)
        try:
            await self.client.disconnect()
        except Exception:
            pass


async def connect(addr: str = "", **_) -> BleClientTransport:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as e:
        raise TransportClosed("BLE host needs `pip install bleak`") from e

    target = addr or DEVICE_NAME
    device = await BleakScanner.find_device_by_address(target, timeout=10.0) if ":" in target \
        else await BleakScanner.find_device_by_name(target, timeout=10.0)
    if device is None:
        raise TransportClosed(f"BLE device '{target}' not found")
    t_holder: dict = {}

    def on_disconnect(_client):
        t = t_holder.get("t")
        if t is not None:
            t.queue.put_nowait(None)

    client = BleakClient(device, disconnected_callback=on_disconnect)
    await client.connect()
    t = BleClientTransport(client)
    t_holder["t"] = t
    await client.start_notify(TX_UUID, lambda _c, data: t.on_fragment(data))
    return t
