"""Unit tests for the frame format, statistics, clock sync and framing helpers."""
import random

import pytest

from linkbench.common import packet
from linkbench.common.clocksync import ClockSync, PingSample
from linkbench.common.profiles import StreamSpec
from linkbench.common.stats import Rec, StreamStats, percentile
from linkbench.common.transports.base import LengthPrefixReader, length_prefixed
from linkbench.common.transports.ble import Reassembler, fragment


# ── packet ────────────────────────────────────────────────────────────────────

def test_roundtrip_and_pattern():
    payload = packet.pattern(3, 42, 500)
    buf = packet.encode(3, 42, payload, packet.FLAG_PATTERN, t_send_ns=123456789)
    f = packet.decode(buf)
    assert (f.stream, f.seq, f.t_send_ns, f.payload) == (3, 42, 123456789, payload)
    assert f.crc_ok and packet.pattern_ok(f) is True
    assert f.size == len(buf) == len(payload) + packet.OVERHEAD


def test_corruption_is_detected():
    buf = bytearray(packet.encode(1, 7, packet.pattern(1, 7, 100), packet.FLAG_PATTERN))
    buf[packet.HEADER_SIZE + 10] ^= 0xFF
    f = packet.decode(bytes(buf))
    assert not f.crc_ok
    assert packet.pattern_ok(f) is False


def test_real_payload_has_no_pattern_check():
    f = packet.decode(packet.encode(1, 0, b"\x01\x02"))
    assert packet.pattern_ok(f) is None


def test_control_message():
    f = packet.decode(packet.control({"type": "ping", "id": 5}))
    assert f.is_control and f.json() == {"type": "ping", "id": 5}


def test_short_and_truncated_frames_raise():
    with pytest.raises(packet.FrameError):
        packet.decode(b"LB1")
    with pytest.raises(packet.FrameError):
        packet.decode(packet.encode(1, 0, b"abcdef")[:-3])


def test_pattern_differs_per_seq():
    assert packet.pattern(1, 1, 64) != packet.pattern(1, 2, 64)


# ── stats ─────────────────────────────────────────────────────────────────────

def _stats(seqs, period_ms=10.0, latency_ms=5.0, offset_ns=1_000_000):
    spec = StreamSpec("imu_left", 1, "imu", 1000.0 / period_ms, 12)
    st = StreamStats(1, "imu_left", spec)
    for i, s in enumerate(seqs):
        t_send_agent = int(s * period_ms * 1e6) + offset_ns          # agent clock
        t_recv = int(s * period_ms * 1e6 + latency_ms * 1e6 + i)     # host clock
        st.add(Rec(s, t_send_agent, t_recv, 30, True, True))
    return st


def test_loss_duplicates_reordering():
    seqs = [s for s in range(100) if s not in (10, 11, 50)]
    seqs.insert(20, 5)          # duplicate
    seqs[30], seqs[31] = seqs[31], seqs[30]   # reorder
    s = _stats(seqs).summary(lambda t: 1_000_000)
    assert s["lost"] == 3
    assert s["duplicates"] == 1
    assert s["reordered"] >= 1
    assert s["loss_pct"] == pytest.approx(3.0)


def test_tail_loss_uses_agent_last_seq():
    st = _stats(list(range(90)))
    st.last_sent_seq = 99
    s = st.summary(lambda t: 1_000_000, final=True)
    assert s["lost"] == 10


def test_latency_rate_and_pass():
    s = _stats(list(range(200))).summary(lambda t: 1_000_000)
    assert s["latency_ms_p50"] == pytest.approx(5.0, abs=0.01)
    assert s["recv_hz"] == pytest.approx(100.0, rel=0.01)
    assert s["sender_hz"] == pytest.approx(100.0, rel=0.01)
    assert s["passed"] is True


def test_threshold_failures():
    seqs = [s for s in range(100) if s % 10]         # 10 % loss
    s = _stats(seqs, latency_ms=150).summary(lambda t: 1_000_000)
    assert s["checks"]["loss"] is False
    assert s["checks"]["latency"] is False
    assert s["passed"] is False


def test_percentile():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([], 50) is None


# ── clock sync ────────────────────────────────────────────────────────────────

def test_clock_offset_and_drift_recovered():
    rng = random.Random(1)
    true_offset_ns, drift = 7_500_000.0, 40e-6        # 7.5 ms, 40 ppm
    cs = ClockSync()
    t = 0
    for _ in range(600):                               # 60 s of pings at 10 Hz
        t += 100_000_000
        up = 500_000 + rng.expovariate(1 / 300_000)    # queueing on top of 0.5 ms
        down = 500_000 + rng.expovariate(1 / 300_000)
        agent = lambda h: h + true_offset_ns + drift * h
        t0 = t
        t1 = int(agent(t0 + up))
        t2 = t1 + 50_000
        t3 = int(t0 + up + 50_000 / (1 + drift) + down)
        cs.add(PingSample(t0, t1, t2, t3))
    assert cs.drift_ppm() == pytest.approx(40, abs=5)
    expected = true_offset_ns + drift * 30e9
    assert cs.offset_at(30e9) == pytest.approx(expected, abs=150_000)   # within 0.15 ms


# ── framing helpers ───────────────────────────────────────────────────────────

def test_length_prefix_reader_handles_split_chunks():
    frames = [packet.encode(1, i, packet.pattern(1, i, 300)) for i in range(5)]
    stream = b"".join(length_prefixed(f) for f in frames)
    r = LengthPrefixReader()
    out = []
    for i in range(0, len(stream), 37):
        out += r.feed(stream[i:i + 37])
    assert out == frames


def test_ble_fragment_roundtrip_and_drop():
    data = packet.encode(4, 1, packet.pattern(4, 1, 5000))
    r = Reassembler()
    frags = fragment(1, data, 180)
    out = [r.feed(f) for f in frags]
    assert out[-1] == data and all(o is None for o in out[:-1])
    frags2 = fragment(2, data, 180)
    for f in frags2[:-1]:
        r.feed(f)                       # last fragment lost
    for f in fragment(3, data, 180):
        last = r.feed(f)
    assert last == data and r.dropped == 1
