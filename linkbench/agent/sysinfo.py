"""Pi health and link information, read from /proc, /sys and standard tools.

Every field degrades to None when unavailable, so the agent also runs on a laptop for
local testing.
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def _read(path: str) -> Optional[str]:
    try:
        return Path(path).read_text().strip("\x00\n ")
    except OSError:
        return None


def _run(*cmd: str) -> Optional[str]:
    if not shutil.which(cmd[0]):
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None


def board_model() -> str:
    return _read("/proc/device-tree/model") or f"{platform.system()} {platform.machine()}"


class CpuMeter:
    """CPU utilisation from /proc/stat deltas."""

    def __init__(self) -> None:
        self._last = self._times()

    @staticmethod
    def _times():
        line = _read("/proc/stat")
        if not line:
            return None
        parts = [int(x) for x in line.splitlines()[0].split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return idle, sum(parts)

    def percent(self) -> Optional[float]:
        now = self._times()
        if now is None or self._last is None:
            return None
        d_idle, d_total = now[0] - self._last[0], now[1] - self._last[1]
        self._last = now
        return round(100.0 * (1 - d_idle / d_total), 1) if d_total > 0 else None


def temperature_c() -> Optional[float]:
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    return round(int(t) / 1000.0, 1) if t and t.isdigit() else None


def throttled() -> Optional[str]:
    out = _run("vcgencmd", "get_throttled")
    if not out:
        return None
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", out)
    return m.group(1) if m else None


def mem_available_mb() -> Optional[float]:
    info = _read("/proc/meminfo")
    if not info:
        return None
    m = re.search(r"MemAvailable:\s+(\d+) kB", info)
    return round(int(m.group(1)) / 1024, 1) if m else None


def wifi_link(iface: str = "wlan0") -> Optional[dict]:
    out = _run("iw", "dev", iface, "link")
    if not out or "Not connected" in out:
        # In AP mode `link` is empty; report the station list instead
        st = _run("iw", "dev", iface, "station", "dump")
        if not st:
            return None
        sig = re.findall(r"signal:\s+(-?\d+)", st)
        tx = re.findall(r"tx bitrate:\s+([\d.]+) MBit/s", st)
        return {"mode": "ap", "stations": len(sig),
                "signal_dbm": int(sig[0]) if sig else None,
                "tx_bitrate_mbps": float(tx[0]) if tx else None}
    def grab(pat, conv=str):
        m = re.search(pat, out)
        return conv(m.group(1)) if m else None
    return {"mode": "client", "ssid": grab(r"SSID:\s+(.+)"),
            "freq_mhz": grab(r"freq:\s+(\d+)", int),
            "signal_dbm": grab(r"signal:\s+(-?\d+)", int),
            "tx_bitrate_mbps": grab(r"tx bitrate:\s+([\d.]+)", float)}


def interfaces() -> dict:
    """Interface name → IPv4 address."""
    out = _run("ip", "-o", "-4", "addr", "show")
    result = {}
    if out:
        for line in out.splitlines():
            m = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)/", line)
            if m:
                result[m.group(1)] = m.group(2)
    return result


def interface_for(local_addr: Optional[str]) -> Optional[str]:
    if not local_addr:
        return None
    ip = local_addr.rsplit(":", 1)[0]
    for name, addr in interfaces().items():
        if addr == ip:
            return name
    return None


class SysInfo:
    def __init__(self) -> None:
        self.cpu = CpuMeter()
        self.model = board_model()

    def snapshot(self) -> dict:
        return {
            "board": self.model,
            "cpu_pct": self.cpu.percent(),
            "temp_c": temperature_c(),
            "throttled": throttled(),
            "mem_available_mb": mem_available_mb(),
            "wifi": wifi_link(),
        }
