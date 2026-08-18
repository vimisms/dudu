"""In-process reminders, exposed to the agent as a tool.

Deliberately NOT an MCP server. Every other tool here runs as a subprocess over
stdio, which is fine for things that just return text -- but a reminder has to
reach back into this process when it fires, to push a sound and speech over the
WebSocket. A subprocess can't do that without inventing a callback channel, so
this is a plain LangChain tool living in the backend, appended to the MCP tool
list in mcp_clients.get_agent_tools().
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from langchain_core.tools import StructuredTool
from loguru import logger
from pydantic import BaseModel, Field

from state import AgentState
from ws_manager import manager

MAX_DELAY_MINUTES = 24 * 60  # a day; anything longer wants real persistence


@dataclass
class Reminder:
    id: str
    text: str
    due_at: float
    task: asyncio.Task | None = field(default=None, repr=False)


_reminders: dict[str, Reminder] = {}


def snapshot() -> list[dict]:
    now = time.time()
    return [
        {"id": r.id, "text": r.text, "in_seconds": max(0, round(r.due_at - now))}
        for r in sorted(_reminders.values(), key=lambda r: r.due_at)
    ]


async def _fire(reminder: Reminder) -> None:
    """Wait, then chime and say the reminder out loud."""
    try:
        await asyncio.sleep(max(0.0, reminder.due_at - time.time()))
    except asyncio.CancelledError:
        return

    _reminders.pop(reminder.id, None)
    logger.info("Reminder firing: {!r}", reminder.text[:80])

    # Sound first, speech second -- the chime is what pulls your attention back
    # to the machine; speaking underneath an unheard cue just wastes the words.
    await manager.send_sound("reminder")
    await manager.send_state(AgentState.TALKING, detail="reminder")
    await manager.broadcast({"type": "reminder", "text": reminder.text})

    try:
        from voice.tts import synthesize  # noqa: PLC0415 - lazy, keeps import cycles out

        wav = await synthesize(f"Reminder. {reminder.text}")
        # Small gap so the chime isn't still ringing under the first word.
        await asyncio.sleep(0.9)
        await manager.send_audio(wav)
    except Exception:  # noqa: BLE001 - the on-screen reminder already landed
        logger.exception("Could not speak reminder")

    await manager.send_state(AgentState.SLEEPING, detail="reminder delivered")


class _SetReminderArgs(BaseModel):
    minutes: float = Field(description="How many minutes from now to remind the user.")
    text: str = Field(
        description=(
            "What to remind them about, phrased to be SPOKEN back to them later, "
            "in the second person. E.g. 'check whether the inventory close finished'."
        )
    )


async def _set_reminder(minutes: float, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "I need to know what the reminder is about."
    if minutes <= 0:
        return "The reminder time has to be in the future."
    if minutes > MAX_DELAY_MINUTES:
        return f"I can only set reminders up to {MAX_DELAY_MINUTES // 60} hours ahead."

    reminder = Reminder(id=uuid.uuid4().hex[:8], text=text, due_at=time.time() + minutes * 60)
    reminder.task = asyncio.create_task(_fire(reminder), name=f"reminder-{reminder.id}")
    _reminders[reminder.id] = reminder

    await manager.broadcast(
        {"type": "reminder_set", "id": reminder.id, "text": text, "in_minutes": minutes}
    )
    logger.info("Reminder set for {:.0f} min: {!r}", minutes, text[:80])

    pretty = f"{minutes:.0f} minutes" if minutes >= 1 else f"{minutes * 60:.0f} seconds"
    return f"Reminder set for {pretty} from now: {text}"


async def _list_reminders() -> str:
    pending = snapshot()
    if not pending:
        return "No reminders pending."
    return "Pending reminders:\n" + "\n".join(
        f"- in {r['in_seconds'] // 60}m {r['in_seconds'] % 60}s: {r['text']}" for r in pending
    )


async def _cancel_reminders() -> str:
    n = 0
    for reminder in list(_reminders.values()):
        if reminder.task and not reminder.task.done():
            reminder.task.cancel()
            n += 1
        _reminders.pop(reminder.id, None)
    return f"Cancelled {n} reminder(s)." if n else "There were no reminders to cancel."


def build_local_tools() -> list:
    """Tools that live in this process rather than behind an MCP server."""
    return [
        StructuredTool.from_function(
            coroutine=_set_reminder,
            name="set_reminder",
            description=(
                "Set a reminder that will chime and be spoken aloud after a delay. "
                "Use whenever the user says anything like 'remind me in 20 minutes to X', "
                "'ping me in an hour about Y', or 'don't let me forget Z'. Convert their "
                "phrasing to minutes (an hour = 60). Confirm briefly in your reply."
            ),
            args_schema=_SetReminderArgs,
        ),
        StructuredTool.from_function(
            coroutine=_list_reminders,
            name="list_reminders",
            description="List reminders that haven't fired yet, with time remaining.",
        ),
        StructuredTool.from_function(
            coroutine=_cancel_reminders,
            name="cancel_reminders",
            description="Cancel every pending reminder. Use when the user says to forget or cancel them.",
        ),
    ]
