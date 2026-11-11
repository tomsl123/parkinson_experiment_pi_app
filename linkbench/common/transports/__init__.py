"""Transport registry. Imports are lazy so missing optional packages only matter when used."""
from __future__ import annotations

import importlib

NAMES = ("ws", "udp", "rfcomm", "ble")

DEFAULT_PORTS = {"ws": 8765, "udp": 8766, "rfcomm": 1, "ble": None}


def module(name: str):
    if name not in NAMES:
        raise ValueError(f"unknown transport '{name}', choose from {', '.join(NAMES)}")
    return importlib.import_module(f"{__name__}.{name}")


async def serve(name: str, handler, **opts):
    return await module(name).serve(handler, **opts)


async def connect(name: str, **opts):
    return await module(name).connect(**opts)
