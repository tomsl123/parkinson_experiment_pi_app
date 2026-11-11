"""Run the linkbench agent (on the Pi).

    python -m linkbench.agent                       # WebSocket :8765 and UDP :8766
    python -m linkbench.agent --transports ws,udp,rfcomm,ble
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from ..common import transports
from .agent import Agent
from .sysinfo import board_model, interfaces

log = logging.getLogger("linkbench.agent")


async def main(args) -> None:
    agent = Agent()
    opts = {
        "ws": {"host": args.host, "port": args.ws_port},
        "udp": {"host": args.host, "port": args.udp_port},
        "rfcomm": {"channel": args.rfcomm_channel},
        "ble": {},
    }

    async def run_one(name: str):
        try:
            log.info("listening: %s %s", name, opts[name])
            await transports.serve(name, agent.handle, **opts[name])
        except Exception as e:
            log.error("transport %s unavailable: %s", name, e)

    names = [n.strip() for n in args.transports.split(",") if n.strip()]
    log.info("board: %s | interfaces: %s", board_model(), interfaces() or "n/a")
    await asyncio.gather(*(run_one(n) for n in names))


def cli() -> None:
    p = argparse.ArgumentParser(description="linkbench agent (runs on the Pi)")
    p.add_argument("--transports", default="ws,udp", help="comma list of ws,udp,rfcomm,ble")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--ws-port", type=int, default=8765)
    p.add_argument("--udp-port", type=int, default=8766)
    p.add_argument("--rfcomm-channel", type=int, default=1)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
