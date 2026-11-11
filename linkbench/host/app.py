"""Dashboard server: one benchmark at a time, live updates to every open browser tab."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..common.profiles import DEFAULT_THRESHOLDS, presets
from ..common.transports import DEFAULT_PORTS, NAMES
from .recorder import RUNS_DIR, list_runs, load_run
from .report import build_report_html
from .session import BenchSession, RunConfig

log = logging.getLogger("linkbench.host")
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="linkbench")
clients: set[WebSocket] = set()
state: dict = {"session": None, "task": None, "latest": None}


async def broadcast(msg: dict) -> None:
    state["latest"] = msg
    raw = json.dumps(msg)
    dead = set()
    for ws in list(clients):
        try:
            await ws.send_text(raw)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)


def _running() -> bool:
    task: Optional[asyncio.Task] = state["task"]
    return task is not None and not task.done()


@app.get("/api/meta")
async def meta():
    return {"presets": {k: v.to_dict() for k, v in presets().items()},
            "thresholds": DEFAULT_THRESHOLDS, "transports": list(NAMES),
            "default_ports": DEFAULT_PORTS, "running": _running()}


@app.get("/api/runs")
async def runs():
    return list_runs()


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str):
    if not (RUNS_DIR / run_id / "summary.json").exists():
        raise HTTPException(404, "run not found")
    return load_run(run_id)


@app.get("/api/runs/{run_id}/report", response_class=HTMLResponse)
async def run_report(run_id: str):
    if not (RUNS_DIR / run_id / "summary.json").exists():
        raise HTTPException(404, "run not found")
    return await asyncio.to_thread(build_report_html, run_id)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({"t": "state", "running": _running()}))
    if state["latest"] and _running():
        await ws.send_text(json.dumps(state["latest"]))
    try:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            if msg.get("cmd") == "start":
                if _running():
                    await ws.send_text(json.dumps({"t": "error", "message": "a run is already in progress"}))
                    continue
                cfg = RunConfig.from_dict(msg.get("config", {}))
                session = BenchSession(cfg, on_update=broadcast)
                state["session"] = session
                state["task"] = asyncio.create_task(session.run())
                await broadcast({"t": "state", "running": True, "run_id": session.run_id})
            elif msg.get("cmd") == "stop" and state["session"] is not None:
                state["session"].stop()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
