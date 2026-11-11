"""Per-stream link metrics: loss, duplicates, reordering, integrity, rates, jitter, latency.

The host records every received frame; summaries are computed on demand, either over a
recent window (live dashboard) or over the whole run (final report). Sender-side rate
(from the agent's send timestamps) is reported separately from the received rate, so a
slow sender is not mistaken for a bad link.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from .profiles import DEFAULT_THRESHOLDS, StreamSpec


def percentile(sorted_vals: list, p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(sorted_vals[lo])
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _mean_std(vals: list) -> tuple[Optional[float], Optional[float]]:
    if not vals:
        return None, None
    m = sum(vals) / len(vals)
    if len(vals) < 2:
        return m, 0.0
    return m, math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


@dataclass
class Rec:
    seq: int
    t_send: int
    t_recv: int
    size: int
    crc_ok: bool
    pattern_ok: Optional[bool]
    step: int = 0


@dataclass
class StreamStats:
    stream: int
    name: str
    spec: Optional[StreamSpec] = None
    recs: list = field(default_factory=list)
    last_sent_seq: Optional[int] = None     # reported by the agent at stop

    def add(self, rec: Rec) -> None:
        self.recs.append(rec)

    def summary(self, offset_fn: Callable[[float], Optional[float]],
                since_ns: Optional[int] = None, step: Optional[int] = None,
                thresholds: Optional[dict] = None, final: bool = False) -> dict:
        th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        recs = self.recs
        if since_ns is not None:
            recs = [r for r in recs if r.t_recv >= since_ns]
        if step is not None:
            recs = [r for r in recs if r.step == step]

        nominal_fps = self.spec.frame_rate if self.spec else None
        batch = self.spec.batch if self.spec else 1
        out: dict = {
            "stream": self.stream, "name": self.name,
            "nominal_hz": self.spec.rate_hz if self.spec else None,
            "nominal_fps": nominal_fps,
            "received": len(recs),
        }
        if not recs:
            out.update(lost=None, loss_pct=None, passed=None)
            return out

        seqs = [r.seq for r in recs]
        unique = set(seqs)
        lo, hi = min(unique), max(unique)
        if final and since_ns is None and step is None:
            lo = 0
            if self.last_sent_seq is not None:
                hi = max(hi, self.last_sent_seq)
        expected = hi - lo + 1
        lost = max(0, expected - len(unique))

        reordered, max_seen = 0, -1
        for s in seqs:
            if s < max_seen:
                reordered += 1
            max_seen = max(max_seen, s)

        crc_err = sum(1 for r in recs if not r.crc_ok)
        pat_err = sum(1 for r in recs if r.pattern_ok is False)

        # Latency (one-way, via clock offset)
        lat = []
        for r in recs:
            off = offset_fn(r.t_recv)
            if off is not None:
                lat.append((r.t_recv - (r.t_send - off)) / 1e6)
        lat.sort()

        # Received timing (arrival order)
        t_recv = [r.t_recv for r in recs]
        gaps = [(b - a) / 1e6 for a, b in zip(t_recv, t_recv[1:])]
        gap_mean, gap_std = _mean_std(gaps)
        gaps_sorted = sorted(gaps)
        period_ms = 1000.0 / nominal_fps if nominal_fps else None
        stalls = sum(1 for g in gaps if period_ms and g > 5 * period_ms)
        span_recv = (t_recv[-1] - t_recv[0]) / 1e9
        recv_fps = (len(unique) - 1) / span_recv if span_recv > 0 else None

        # Sender timing (sequence order) – separates sender problems from link problems
        by_seq = sorted({r.seq: r.t_send for r in recs}.items())
        send_span = (by_seq[-1][1] - by_seq[0][1]) / 1e9 if len(by_seq) > 1 else 0
        seq_span = by_seq[-1][0] - by_seq[0][0] if len(by_seq) > 1 else 0
        sender_fps = seq_span / send_span if send_span > 0 else None

        nbytes = sum(r.size for r in recs)
        throughput = nbytes / span_recv / 1000.0 if span_recv > 0 else None

        loss_pct = 100.0 * lost / expected if expected else 0.0
        rate_err = (100.0 * (recv_fps - nominal_fps) / nominal_fps
                    if (recv_fps is not None and nominal_fps) else None)
        sender_err = (100.0 * (sender_fps - nominal_fps) / nominal_fps
                      if (sender_fps is not None and nominal_fps) else None)
        p95 = percentile(lat, 95)

        checks = {
            "loss": loss_pct <= th["max_loss_pct"],
            "rate": rate_err is None or abs(rate_err) <= th["max_rate_error_pct"],
            "latency": (p95 is None or not (self.spec and self.spec.live)
                        or p95 <= th["max_p95_latency_ms"]),
            "integrity": crc_err + pat_err <= th["max_integrity_errors"],
        }
        out.update(
            unique=len(unique), expected=expected, lost=lost, loss_pct=round(loss_pct, 4),
            duplicates=len(seqs) - len(unique), reordered=reordered,
            crc_errors=crc_err, pattern_errors=pat_err,
            recv_hz=round(recv_fps * batch, 3) if recv_fps else None,
            sender_hz=round(sender_fps * batch, 3) if sender_fps else None,
            rate_error_pct=round(rate_err, 3) if rate_err is not None else None,
            sender_error_pct=round(sender_err, 3) if sender_err is not None else None,
            interarrival_ms_mean=_r(gap_mean), interarrival_ms_std=_r(gap_std),
            interarrival_ms_p95=_r(percentile(gaps_sorted, 95)),
            interarrival_ms_max=_r(gaps_sorted[-1] if gaps_sorted else None),
            stalls=stalls,
            latency_ms_p50=_r(percentile(lat, 50)), latency_ms_p95=_r(p95),
            latency_ms_p99=_r(percentile(lat, 99)), latency_ms_max=_r(lat[-1] if lat else None),
            latency_ms_min=_r(lat[0] if lat else None),
            throughput_kBps=_r(throughput), bytes=nbytes,
            checks=checks, passed=all(checks.values()),
        )
        return out


def _r(v, nd=3):
    return None if v is None else round(v, nd)


def histogram(values: list, bins: int = 30, lo: Optional[float] = None,
              hi: Optional[float] = None) -> dict:
    if not values:
        return {"edges": [], "counts": []}
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    if hi <= lo:
        hi = lo + 1e-9
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        i = int((v - lo) / width)
        counts[min(max(i, 0), bins - 1)] += 1
    return {"edges": [lo + i * width for i in range(bins + 1)], "counts": counts}
