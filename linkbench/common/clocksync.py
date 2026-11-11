"""NTP-style clock offset estimation between host and agent.

Each ping yields four timestamps: t0 host send, t1 agent receive, t2 agent send,
t3 host receive. Then

    rtt    = (t3 - t0) - (t2 - t1)
    offset = ((t1 - t0) + (t2 - t3)) / 2      ≈ agent_clock - host_clock

Samples with the smallest RTT are the least disturbed by queueing, so the live offset
comes from the minimum-RTT sample in a sliding window, and the drift is a linear fit
through the minimum-RTT sample of each window.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class PingSample:
    t0: int
    t1: int
    t2: int
    t3: int

    @property
    def rtt_ns(self) -> int:
        return (self.t3 - self.t0) - (self.t2 - self.t1)

    @property
    def offset_ns(self) -> float:
        return ((self.t1 - self.t0) + (self.t2 - self.t3)) / 2.0


class ClockSync:
    def __init__(self, window: int = 20):
        self.window = window
        self.samples: list[PingSample] = []
        self._recent: deque[PingSample] = deque(maxlen=window)
        self._anchors: list[tuple[float, float]] = []   # (host time, offset) per window
        self._since_anchor = 0
        self.sent = 0

    def add(self, s: PingSample) -> None:
        if s.rtt_ns < 0:
            return
        self.samples.append(s)
        self._recent.append(s)
        self._since_anchor += 1
        if self._since_anchor >= self.window:
            best = min(self._recent, key=lambda x: x.rtt_ns)
            self._anchors.append(((best.t0 + best.t3) / 2.0, best.offset_ns))
            self._since_anchor = 0

    @property
    def ready(self) -> bool:
        return bool(self._recent)

    def offset_now(self) -> Optional[float]:
        if not self._recent:
            return None
        return min(self._recent, key=lambda x: x.rtt_ns).offset_ns

    def drift_fit(self) -> Optional[tuple[float, float]]:
        """(offset at t=0, slope) of offset over host time, or None with < 2 anchors."""
        pts = self._anchors
        if len(pts) < 2:
            return None
        n = len(pts)
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        sxx = sum((p[0] - mx) ** 2 for p in pts)
        if sxx == 0:
            return None
        slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx
        return my - slope * mx, slope

    def drift_ppm(self) -> Optional[float]:
        fit = self.drift_fit()
        return None if fit is None else fit[1] * 1e6

    def offset_at(self, host_t: float) -> Optional[float]:
        """Best offset for a given host time: the drift fit if available, else the live value."""
        fit = self.drift_fit()
        if fit is not None:
            return fit[0] + fit[1] * host_t
        return self.offset_now()

    def rtts_ms(self) -> list[float]:
        return [s.rtt_ns / 1e6 for s in self.samples]
