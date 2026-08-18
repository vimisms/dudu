"""Concurrent background task manager.

Each user instruction (typed or spoken) becomes a Task that runs the LangGraph
agent in its own asyncio task, so multiple instructions process in parallel.
The manager is UI-agnostic: it just broadcasts task lifecycle events over the
WebSocket (ConnectionManager), and the frontend reflects them (task list on the
main window, rich-text output on the results window).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field

from loguru import logger

from agent_graph import run_agent, summarize_result
from config import settings
from state import AgentState
from voice.tts import synthesize
from ws_manager import manager

TASK_TIMEOUT_S = int(settings.task_timeout_s)
SUMMARY_TIMEOUT_S = int(settings.summary_timeout_s)
HISTORY_TURNS = max(0, int(settings.history_turns))


@dataclass
class Task:
    id: str
    instruction: str
    source: str  # "text" | "voice"
    status: str = "queued"  # queued | running | done | error | cancelled
    output: str = ""        # full agent answer (markdown)
    summary: str = ""       # 4-5 line summary shown in the results window
    phase: str = "Waiting to start"
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class TaskManager:
    def __init__(self, max_history: int = 50) -> None:
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []          # insertion order of task ids
        self._runners: dict[str, asyncio.Task] = {}
        self._max_history = max_history
        self.muted: bool = False
        # Recent (role, text) exchanges replayed into each new task so that
        # follow-up questions have a referent -- see agent_graph._build_messages.
        self._exchanges: deque[tuple[str, str]] = deque(maxlen=HISTORY_TURNS * 2 or 1)
        # asyncio.create_task returns a task the event loop only holds WEAKLY.
        # Without keeping our own reference, a fire-and-forget coroutine (the
        # "Okay, I'm on it" TTS) can be garbage-collected mid-flight and its
        # exceptions swallowed. Keep them until they finish.
        self._background: set[asyncio.Task] = set()

    # ---- state helpers ------------------------------------------------------

    def _spawn(self, coro, name: str | None = None) -> asyncio.Task:
        """Fire-and-forget, but with a strong reference held until completion."""
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _history(self) -> list[tuple[str, str]]:
        return list(self._exchanges) if HISTORY_TURNS else []

    def _remember(self, instruction: str, answer: str) -> None:
        if not HISTORY_TURNS or not answer:
            return
        self._exchanges.append(("user", instruction))
        # Cap replayed answers: a full triage write-up is thousands of tokens
        # and we only need enough for a pronoun to resolve against.
        self._exchanges.append(("assistant", answer[:1500]))

    def _active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status in ("queued", "running"))

    async def _sync_avatar(self) -> None:
        """Coarse avatar state derived from task activity (cosmetic)."""
        if self._active_count() > 0:
            await manager.send_state(AgentState.THINKING, detail=f"{self._active_count()} task(s) running")
        else:
            await manager.send_state(AgentState.IDLE, detail="ready")

    async def _summarize(self, instruction: str, output: str) -> str:
        """Real LLM summary of the finished answer, with a hard timeout.

        agent_graph.summarize_result existed but was never called -- summaries
        shown in the UI and read aloud were just the first 400 characters of the
        answer, which for a triage write-up meant hearing the preamble and none
        of the mitigation. If the summarizer is slow or errors, fall back to that
        truncation rather than failing an otherwise successful task.
        """
        if not output.strip():
            return "The task finished but produced no output."
        try:
            summary = await asyncio.wait_for(
                summarize_result(instruction, output), timeout=SUMMARY_TIMEOUT_S
            )
            if summary.strip():
                return summary.strip()
        except asyncio.TimeoutError:
            logger.warning("Summarizer timed out after {}s; using truncated summary", SUMMARY_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a summary is never worth failing a task over
            logger.exception("Summarizer failed; using truncated summary")
        return _fallback_summary(output)

    async def _speak(self, text: str) -> None:
        if self.muted:
            return
        try:
            wav = await synthesize(text)
            await manager.send_audio(wav)
        except Exception:  # noqa: BLE001 - audio is best-effort
            logger.exception("TTS synthesis failed")

    # ---- public API ---------------------------------------------------------

    def snapshot(self) -> list[dict]:
        return [self._tasks[i].to_dict() for i in self._order if i in self._tasks]

    def set_muted(self, muted: bool) -> None:
        self.muted = muted

    async def submit(self, instruction: str, source: str = "text") -> Task:
        """Create a task, acknowledge instantly, and kick off background work."""
        instruction = (instruction or "").strip()
        task = Task(id=uuid.uuid4().hex[:12], instruction=instruction, source=source)
        self._tasks[task.id] = task
        self._order.append(task.id)
        self._evict_old()

        await manager.send_task(task.to_dict())
        await manager.send_transcript("user", instruction)
        # Immediate acknowledgement that the instruction landed -- this fires
        # before any model call, so it's the fastest possible feedback that
        # DuDu heard you.
        await manager.send_sound("accepted")
        await self._sync_avatar()

        self._runners[task.id] = asyncio.create_task(self._run(task), name=f"task-{task.id}")
        return task

    async def cancel(self, task_id: str) -> bool:
        runner = self._runners.get(task_id)
        if runner and not runner.done():
            runner.cancel()
            return True
        return False

    async def cancel_all(self) -> int:
        n = 0
        for task_id, runner in list(self._runners.items()):
            if not runner.done():
                runner.cancel()
                n += 1
        return n

    def clear_finished(self) -> None:
        """Drop completed/failed/cancelled tasks from history; keep active ones."""
        active = {"queued", "running"}
        keep = [i for i in self._order if i in self._tasks and self._tasks[i].status in active]
        for i in list(self._tasks):
            if i not in keep:
                self._tasks.pop(i, None)
                self._runners.pop(i, None)
        self._order = keep

    # ---- internals ----------------------------------------------------------

    def _evict_old(self) -> None:
        while len(self._order) > self._max_history:
            old = self._order.pop(0)
            t = self._tasks.get(old)
            # keep anything still running; only evict finished history
            if t and t.status in ("queued", "running"):
                self._order.insert(0, old)
                break
            self._tasks.pop(old, None)
            self._runners.pop(old, None)

    async def _run(self, task: Task) -> None:
        try:
            task.status = "running"
            task.phase = "Starting"
            task.started_at = time.time()
            await manager.send_task(task.to_dict())
            await self._sync_avatar()

            # Acknowledgement audio must never delay the first model request.
            self._spawn(self._speak("Okay, I'm on it."), name=f"ack-{task.id}")

            history = self._history()

            last_stream_update = 0.0

            async def on_chunk(output: str) -> None:
                nonlocal last_stream_update
                task.output = output
                task.phase = "Writing answer"
                now = time.monotonic()
                if now - last_stream_update >= 0.12:
                    last_stream_update = now
                    await manager.send_task(task.to_dict())

            async def on_progress(phase: str) -> None:
                task.phase = phase
                await manager.send_task(task.to_dict())

            # Hard cap: never let a task run forever.
            try:
                output = await asyncio.wait_for(
                    run_agent(
                        task.instruction,
                        thread_id=task.id,
                        on_chunk=on_chunk,
                        on_progress=on_progress,
                        history=history,
                    ),
                    timeout=TASK_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                task.status = "error"
                task.error = f"Timed out after {TASK_TIMEOUT_S}s"
                task.summary = (
                    f"I stopped this task because it ran longer than {TASK_TIMEOUT_S} seconds "
                    "without finishing. This usually means the request was too broad or a tool "
                    "stalled. Try again with a more specific instruction."
                )
                task.finished_at = time.time()
                await manager.send_task(task.to_dict())
                await manager.send_transcript("assistant", task.summary)
                await self._speak("That took too long, so I stopped it. Try a more specific request.")
                return

            task.output = output or ""
            task.status = "done"
            task.finished_at = time.time()
            # Push the finished answer BEFORE summarizing, so the results window
            # shows the real output immediately rather than waiting on a second
            # LLM round-trip.
            task.phase = "Summarizing"
            task.summary = _fallback_summary(task.output)
            await manager.send_task(task.to_dict())

            task.summary = await self._summarize(task.instruction, task.output)
            task.phase = "Complete"
            self._remember(task.instruction, task.output)
            await manager.send_task(task.to_dict())
            # Completion chime before the spoken summary: you may have walked
            # away, and the cue is what brings you back to read the answer.
            await manager.send_sound("done")
            await manager.send_transcript("assistant", task.summary)
            # Speak only a short 1-2 line gist; the full answer stays on screen.
            await self._speak(_spoken_brief(task.summary))

        except asyncio.CancelledError:
            task.status = "cancelled"
            task.finished_at = time.time()
            await manager.send_task(task.to_dict())
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Task {} failed", task.id)
            task.status = "error"
            task.error = str(exc)
            task.summary = f"This task failed: {exc}"
            task.finished_at = time.time()
            await manager.send_task(task.to_dict())
            await manager.send_sound("error")
            await manager.send_transcript("assistant", task.summary)
            await manager.send_error(f"Task failed: {exc}")
        finally:
            self._runners.pop(task.id, None)
            await self._sync_avatar()


def _fallback_summary(output: str, max_lines: int = 5) -> str:
    text = " ".join((output or "").split())
    if not text:
        return "The task finished but produced no output."
    return text[:400] + ("\u2026" if len(text) > 400 else "")


def _spoken_brief(summary: str, max_chars: int = 240) -> str:
    """A short 1-2 sentence version of the summary for TTS (the full text is
    shown on screen, so we don't read the whole thing aloud)."""
    import re

    text = " ".join((summary or "").split()).strip()
    if not text:
        return "Done."
    sentences = re.split(r"(?<=[.!?])\s+", text)
    brief = " ".join(sentences[:2]).strip()
    if len(brief) > max_chars:
        brief = brief[:max_chars].rsplit(" ", 1)[0] + "\u2026"
    return f"Done. {brief}"


task_manager = TaskManager()
