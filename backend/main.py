"""FastAPI backend ("The Brain").

Responsibilities:
- Serves a WebSocket (/ws) that pushes avatar state + transcript + TTS audio
  events to the Tauri frontend, so the UI never blocks on agent/voice work.
- Runs the always-on wake-word/voice loop as a background asyncio task,
  started on app startup, independent of any particular WebSocket client.
- Initializes MCP clients and the LangGraph agent in the background at boot
  (so the first command isn't charged the ~15s model/tool load), and tears
  them down cleanly on shutdown.
- Exposes a small REST surface for debugging (manual text-in, health check)
  without needing the microphone.

Security note: this server has no authentication. It binds to 127.0.0.1 by
default (see config.Settings.ws_host) precisely because the agent behind it can
read your knowledge-base repo and, when those MCP servers are configured, spend
money on your behalf.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from logging_setup import setup_logging

setup_logging()

from agent_graph import agent_is_ready, get_agent  # noqa: E402 - must follow setup_logging
from config import settings  # noqa: E402
from mcp_clients import close_mcp_client  # noqa: E402
from state import AgentState  # noqa: E402
from tasks import task_manager  # noqa: E402
from voice.audio_loop import (  # noqa: E402
    is_microphone_enabled,
    request_stop,
    set_microphone_enabled,
    sleeping_detail,
    start_ptt,
    stop_ptt,
    voice_loop,
)
from ws_manager import manager  # noqa: E402

_voice_task: asyncio.Task | None = None
_warmup_task: asyncio.Task | None = None


async def _warm_up() -> None:
    """Build and cache the LangGraph agent (which loads every MCP server once).

    Runs in the background rather than blocking startup: loading MCP servers can
    take the better part of a minute on a cold npx cache, and there's no reason
    the UI shouldn't be able to connect and show status during that time. If it
    fails, the first command retries it.
    """
    try:
        await get_agent()
        logger.info("Agent warm-up complete")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep serving; first command retries
        logger.error("Agent warm-up failed, will retry on first command: {}", exc)
        await manager.send_error(f"Agent warm-up failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _voice_task, _warmup_task

    _warmup_task = asyncio.create_task(_warm_up(), name="agent_warmup")
    _voice_task = asyncio.create_task(voice_loop(), name="voice_loop")
    logger.info("Voice loop started; serving on {}:{}", settings.ws_host, settings.ws_port)

    yield

    for task in (_voice_task, _warmup_task):
        if task and not task.done():
            task.cancel()
    # Actually wait for the cancellation to land, so the mic stream and the MCP
    # subprocesses get torn down before the interpreter exits -- otherwise stray
    # npx/python children survive a restart holding the audio device and the
    # vector index files open.
    for task in (_voice_task, _warmup_task):
        if task is None:
            continue
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Error while shutting down background task")
    await close_mcp_client()
    logger.info("Backend shut down cleanly")


app = FastAPI(title="DuDu Assistant Backend", lifespan=lifespan)

# The webview origin varies by platform (tauri://localhost on Windows/Linux,
# http://tauri.localhost on some builds) plus the Vite dev server. Listing them
# explicitly instead of "*" stops a random web page you happen to visit from
# POSTing to your agent. (WebSockets aren't subject to CORS -- that's what the
# 127.0.0.1 bind is for.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "http://localhost:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    """start-dudu.bat polls this to wait for the backend before launching the UI."""
    return {
        "status": "ok",
        "agent_ready": agent_is_ready(),
        "active_tasks": sum(1 for t in task_manager.snapshot() if t["status"] in ("queued", "running")),
    }


class TextCommand(BaseModel):
    text: str
    thread_id: str = "default"


@app.post("/command")
async def command(body: TextCommand) -> dict:
    """Debug/entry endpoint: enqueue an instruction as a background task and
    return immediately with an acknowledgement (the agent runs asynchronously;
    results are pushed over the WebSocket / results window)."""
    task = await task_manager.submit(body.text, source="text")
    return {"task_id": task.id, "status": task.status, "ack": "Got it — working on that in the background."}


@app.get("/tasks")
async def list_tasks() -> dict:
    return {"tasks": task_manager.snapshot(), "muted": task_manager.muted}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        # Tell a freshly-connected UI what state we're already in, plus the
        # current task list so a newly-opened window (e.g. results) can render.
        detail = sleeping_detail() if agent_is_ready() else "starting up — loading tools"
        await manager.send_state(AgentState.SLEEPING, detail=detail)
        await manager.send_snapshot(
            websocket,
            task_manager.snapshot(),
            task_manager.muted,
            is_microphone_enabled(),
            settings.voice_mode,
        )
        while True:
            raw = await websocket.receive_text()
            logger.debug("Frontend -> backend: {}", raw)
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            msg_type = data.get("type")
            if msg_type == "command":
                text = (data.get("text") or "").strip()
                if text:
                    await task_manager.submit(text, source="text")
            elif msg_type == "ptt_start":
                # Talk button pressed. Opening the device here (rather than on a
                # mic toggle) is what keeps hold-to-talk honest: the microphone
                # is live only while the button is physically down.
                start_ptt()
                await manager.send_mic(True)
            elif msg_type == "ptt_stop":
                stop_ptt()
            elif msg_type == "mic_toggle":
                enabled = bool(data.get("on"))
                set_microphone_enabled(enabled)
                # Broadcast, not just reply: the results window (a second WS
                # client) must agree about whether the mic is live.
                await manager.send_mic(enabled)
                await manager.send_state(AgentState.SLEEPING, detail=sleeping_detail())
            elif msg_type == "mute":
                task_manager.set_muted(bool(data.get("on")))
            elif msg_type == "cancel_task":
                await task_manager.cancel(str(data.get("id", "")))
            elif msg_type == "clear_tasks":
                task_manager.clear_finished()
                await manager.broadcast(
                    {"type": "snapshot", "tasks": task_manager.snapshot(), "muted": task_manager.muted}
                )
            elif msg_type == "stop":
                await task_manager.cancel_all()
                request_stop()
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError covers the "receive after close" race during rapid
        # frontend reconnects (Vite HMR / StrictMode) -- just clean up.
        await manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    # reload=False on purpose: the reloader forks a second process, which would
    # start a SECOND copy of the always-on mic loop and a second set of MCP
    # subprocesses, fighting over the same audio device and vector index.
    uvicorn.run(app, host=settings.ws_host, port=settings.ws_port, log_config=None)
