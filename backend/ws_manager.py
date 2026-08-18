"""Broadcasts backend state + payloads to every connected Tauri frontend, without
ever letting a slow/dead client block the voice loop (that's the "UI never freezes"
requirement -- the voice/agent pipeline pushes into an asyncio.Queue-backed broadcast
and moves on regardless of whether the frontend is listening)."""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from fastapi import WebSocket
from loguru import logger

from state import AgentState


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
        logger.info("Frontend connected ({} total)", len(self._connections))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(ws)
        logger.info("Frontend disconnected ({} total)", len(self._connections))

    # A client that has stopped reading (minimised window, suspended laptop,
    # frozen webview) will eventually stop draining its send buffer. Bound how
    # long we're willing to wait on any one of them.
    _SEND_TIMEOUT_S = 3.0

    async def _send_one(self, ws: WebSocket, payload: str) -> WebSocket | None:
        """Send to one client; returns the socket if it should be dropped."""
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=self._SEND_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("Dropping a WebSocket client that stopped reading")
            return ws
        except Exception:  # noqa: BLE001 - closed/broken socket
            return ws
        return None

    async def _broadcast(self, message: dict[str, Any]) -> None:
        if not self._connections:
            return
        payload = json.dumps(message)
        # Concurrently, not serially: this is called from the voice loop and the
        # task streamer at up to ~8 messages/sec, and a serial loop meant one
        # slow client added its latency to every other client AND to the audio
        # pipeline behind them. That is exactly the "UI never freezes" property
        # this class exists to guarantee.
        results = await asyncio.gather(
            *(self._send_one(ws, payload) for ws in list(self._connections)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, WebSocket):
                await self.disconnect(result)

    # ---- convenience senders used by the voice loop / agent -----------------

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Public generic broadcast for arbitrary JSON messages."""
        await self._broadcast(message)

    async def send_state(self, state: AgentState, detail: str = "") -> None:
        await self._broadcast({"type": "state", "state": state.value, "detail": detail})

    async def send_transcript(self, role: str, text: str) -> None:
        """role: 'user' or 'assistant' -- lets the UI show a running transcript."""
        await self._broadcast({"type": "transcript", "role": role, "text": text})

    async def send_task(self, task: dict[str, Any]) -> None:
        """Push a single task's full state; the UI upserts it by id."""
        await self._broadcast({"type": "task", "task": task})

    async def send_snapshot(
        self,
        ws: WebSocket,
        tasks: list[dict[str, Any]],
        muted: bool,
        mic: bool = False,
        voice_mode: str = "push_to_talk",
    ) -> None:
        """Sent to a single freshly-connected client so it can render existing
        tasks -- and so its mic/mute toggles show the BACKEND's actual state
        rather than whatever the component happened to initialise to."""
        try:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "snapshot",
                        "tasks": tasks,
                        "muted": muted,
                        "mic": mic,
                        "voice_mode": voice_mode,
                    }
                )
            )
        except Exception:  # noqa: BLE001
            await self.disconnect(ws)

    async def send_sound(self, name: str) -> None:
        """Ask the UI to play a short cue: 'accepted' | 'done' | 'error' | 'reminder'.

        Sent as an event rather than synthesised audio: these are sub-second
        tones the frontend generates with the Web Audio API, so there's no WAV
        to encode, no file to ship, and no latency between the thing happening
        and you hearing it.
        """
        await self._broadcast({"type": "sound", "name": name})

    async def send_mic(self, enabled: bool) -> None:
        """Broadcast a mic on/off change so every window agrees."""
        await self._broadcast({"type": "mic", "on": enabled})

    async def send_audio(self, wav_bytes: bytes) -> None:
        """Push a base64-encoded WAV clip for the frontend to play (Piper TTS output)."""
        b64 = base64.b64encode(wav_bytes).decode("ascii")
        await self._broadcast({"type": "audio", "format": "wav", "data": b64})

    async def send_error(self, message: str) -> None:
        await self._broadcast({"type": "error", "message": message})


manager = ConnectionManager()
