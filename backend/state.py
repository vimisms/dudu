"""Avatar state machine shared by the voice loop, the agent, and the WebSocket layer.

Only 4 GIFs exist on the frontend (idle/listening/thinking/talking). SLEEPING reuses
idle.gif but the frontend dims/pauses it -- see App.jsx.
"""
from __future__ import annotations

import enum


class AgentState(str, enum.Enum):
    SLEEPING = "sleeping"     # wake-word engine running, nothing else active
    IDLE = "idle"             # awake, waiting for a command
    LISTENING = "listening"   # actively recording the user's spoken instruction
    THINKING = "thinking"     # STT done, LangGraph agent + MCP tools are running
    TALKING = "talking"       # streaming Piper TTS audio back to the UI


STATE_TO_GIF = {
    AgentState.SLEEPING: "idle.gif",   # dimmed on the frontend
    AgentState.IDLE: "idle.gif",
    AgentState.LISTENING: "listening.gif",
    AgentState.THINKING: "thinking.gif",
    AgentState.TALKING: "talking.gif",
}
