"""End-to-end: agent and host in one process over localhost, with injected faults.

Checks that the tool measures what is injected. Timing tolerances are generous because
Windows timers tick in ~15.6 ms steps.
"""
import asyncio
import socket

import pytest

from linkbench.agent.agent import Agent
from linkbench.common import transports
from linkbench.host.session import BenchSession, RunConfig


def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_bench(tmp_path, transport, profile, port, **cfg):
    async def main():
        agent = Agent()
        server = asyncio.create_task(transports.serve(transport, agent.handle,
                                                      host="127.0.0.1", port=port))
        await asyncio.sleep(0.3)
        try:
            session = BenchSession(RunConfig(transport=transport, addr="127.0.0.1", port=port,
                                             profile=profile, **cfg), runs_dir=tmp_path)
            return await session.run()
        finally:
            server.cancel()

    return asyncio.run(main())


def by_name(summary):
    return {s["name"]: s for s in summary["streams"]}


def test_ws_clean_link(tmp_path):
    s = run_bench(tmp_path, "ws", "imu", free_port(), duration_s=3)
    assert s["error"] is None
    for st in by_name(s).values():
        assert st["loss_pct"] == 0
        assert st["crc_errors"] == 0 and st["pattern_errors"] == 0
        assert st["recv_hz"] == pytest.approx(100, rel=0.05)
    assert (tmp_path / s["run_id"] / "packets.csv").exists()


def test_udp_injected_loss_delay_corruption(tmp_path):
    s = run_bench(tmp_path, "udp", "imu", free_port(socket.SOCK_DGRAM), duration_s=3,
                  faults={"drop_pct": 10, "delay_ms": 30, "corrupt_pct": 5})
    assert s["error"] is None
    for st in by_name(s).values():
        assert 3 <= st["loss_pct"] <= 18                   # 10 % injected
        assert 25 <= st["latency_ms_p50"] <= 70            # 30 ms injected
        assert st["crc_errors"] > 0                        # corruption caught
        assert st["passed"] is False
    assert s["injected_faults"]["dropped"] > 0


def test_bulk_transfer_hash(tmp_path):
    s = run_bench(tmp_path, "ws", "bulk", free_port(), bulk_bytes=2 * 1024 * 1024)
    assert s["bulk"]["sha256_ok"] is True
    assert s["bulk"]["received_bytes"] == 2 * 1024 * 1024
    assert s["passed"] is True
