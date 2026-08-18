"""OpenWakeWord listener for two custom wake phrases: "wake up" and "go to sleep".

OpenWakeWord ships pretrained models for common phrases ("hey jarvis", "alexa",
etc.) but NOT for arbitrary custom phrases like these -- you need to train (or
fine-tune) your own .onnx/.tflite models and drop them in backend/models/wakeword/.
See https://github.com/dscripka/openWakeWord (training notebooks) --
docs/SETUP.md has the short version. Until you've trained them, this module
falls back to a keyword-spotting stub via Whisper on short rolling buffers so
the rest of the pipeline is runnable end-to-end.
"""
from __future__ import annotations

import asyncio
import re
from collections import deque
from pathlib import Path
from typing import AsyncIterator

import numpy as np
import webrtcvad
from loguru import logger

from config import settings
from state import AgentState
from ws_manager import manager

WAKEWORD_DIR = Path(__file__).resolve().parent.parent / "models" / "wakeword"
SAMPLE_RATE = 16000
FRAME_MS = 80  # openwakeword expects ~80ms (1280 sample) chunks at 16kHz


class WakeWordDetector:
    """Wraps openwakeword.Model, yielding "wake_up" / "go_to_sleep" events."""

    def __init__(self) -> None:
        self._model = None
        self._labels: dict[str, str] = {}

    def _lazy_load(self) -> None:
        if self._model is not None:
            return
        import openwakeword
        from openwakeword.model import Model

        on_path = WAKEWORD_DIR / f"{settings.wake_word_on}.onnx"
        off_path = WAKEWORD_DIR / f"{settings.wake_word_off}.onnx"
        model_paths = [p for p in (on_path, off_path) if p.exists()]

        if not model_paths:
            logger.warning(
                "No custom wakeword models found in {}. Train 'wake up' / 'go to "
                "sleep' models first (see docs/SETUP.md) -- falling back to "
                "openwakeword's bundled demo models for now.",
                WAKEWORD_DIR,
            )
            openwakeword.utils.download_models()
            self._model = Model()  # loads all bundled pretrained models
        else:
            self._model = Model(wakeword_models=[str(p) for p in model_paths])

        for path in model_paths:
            self._labels[path.stem] = path.stem
        logger.info("Wake word models loaded: {}", list(self._model.models.keys()))

    async def listen(self, mic_stream: AsyncIterator[np.ndarray]) -> AsyncIterator[str]:
        """Consumes int16 mono 16kHz audio frames, yields wake-word labels as
        they cross the detection threshold (debounced)."""
        self._lazy_load()
        cooldown_until = 0.0
        _last_diag = 0.0

        async for frame in mic_stream:
            predictions = await asyncio.to_thread(self._model.predict, frame)
            now = asyncio.get_event_loop().time()
            # TEMP diagnostic: every ~1s log the loudest wake candidate + mic level.
            if predictions and now - _last_diag > 1.0:
                _last_diag = now
                top_label, top_score = max(predictions.items(), key=lambda kv: kv[1])
                rms = float(np.sqrt(np.mean((frame.astype(np.float32)) ** 2)))
                logger.debug("wake diag: top={} score={:.2f} mic_rms={:.0f}", top_label, top_score, rms)
            for label, score in predictions.items():
                if score > settings.wake_threshold and now > cooldown_until:
                    cooldown_until = now + 2.0  # avoid re-triggering on the same utterance
                    logger.info("Wake word '{}' detected (score {:.2f})", label, score)
                    normalized = _normalize_label(label, settings)
                    if normalized:
                        yield normalized


def _normalize_label(label: str, settings) -> str | None:
    lowered = label.lower()
    if settings.wake_word_on.replace("_", " ") in lowered or lowered == settings.wake_word_on:
        return "wake_up"
    if settings.wake_word_off.replace("_", " ") in lowered or lowered == settings.wake_word_off:
        return "go_to_sleep"
    return None


# Pronunciation variants Whisper might produce for custom wake words.
_WAKE_VARIANTS = {
    "dudu": ("dudu", "du du", "doodoo", "doo doo", "dodo", "do do", "doe doe",
             "dou dou", "dho dho", "doto", "dodoe", "dude", "doodle", "dudu's"),
    "hey dudu": ("hey dudu", "hey dodo", "hey doodoo", "hey do do", "hey dude"),
}


class WhisperWakeDetector:
    """Wake detection for ANY custom phrase (e.g. "dudu") by transcribing short
    speech snippets with Whisper. VAD-gated, so Whisper only runs when you
    actually speak, not on silence.

    If the wake word appears at the START of an utterance that also carries a
    command ("dudu find the sql view"), the trailing command is captured in the
    same breath (exposed as `pending_command`) so you don't have to wait for a
    second listen step."""

    VAD_FRAME_SAMPLES = int(SAMPLE_RATE * 30 / 1000)
    # Trailing silence that ends an utterance. This is pure dead time on EVERY
    # turn -- you stop talking and nothing can happen until it elapses -- so
    # it's the first dial to turn if wake feels sluggish. Too low and it cuts
    # you off mid-sentence. Configurable via WAKE_TRAILING_SILENCE_MS.
    TRAILING_SILENCE_MS = int(settings.wake_trailing_silence_ms)
    MIN_RMS = 120

    def __init__(self) -> None:
        self._vad = webrtcvad.Vad(2)
        self.pending_command = ""  # command spoken in the same breath as the wake word
        logger.info(
            "Whisper wake detector active (wake='{}', sleep='{}')",
            settings.wake_word_on, settings.wake_word_off,
        )

    @staticmethod
    def _phrases(raw: str) -> tuple[str, ...]:
        key = raw.lower().replace("_", " ").strip()
        return _WAKE_VARIANTS.get(key, (key,))

    @staticmethod
    def _normalize(text: str) -> str:
        norm = re.sub(r"[^a-z0-9 ]", " ", text.lower())
        return re.sub(r"\s+", " ", norm).strip()

    async def _collect_utterance(self, mic_stream: AsyncIterator[np.ndarray]) -> np.ndarray:
        """Waits for speech (VAD), captures it plus a short pre-roll, and returns
        once ~TRAILING_SILENCE_MS of silence follows."""
        preroll: deque[np.ndarray] = deque(maxlen=5)  # ~400ms lead-in
        collected: list[np.ndarray] = []
        started = False
        silence_ms = 0
        leftover = np.empty(0, dtype=np.int16)

        async for frame in mic_stream:
            buf = np.concatenate([leftover, frame])
            n = len(buf) // self.VAD_FRAME_SAMPLES
            frame_has_speech = False
            for i in range(n):
                chunk = buf[i * self.VAD_FRAME_SAMPLES : (i + 1) * self.VAD_FRAME_SAMPLES]
                if self._vad.is_speech(chunk.tobytes(), SAMPLE_RATE):
                    frame_has_speech = True
                    silence_ms = 0
                elif started:
                    silence_ms += 30
            leftover = buf[n * self.VAD_FRAME_SAMPLES :]

            if started:
                collected.append(frame)
                if silence_ms >= self.TRAILING_SILENCE_MS:
                    break
            else:
                preroll.append(frame)
                if frame_has_speech:
                    started = True
                    collected.extend(preroll)
                    # Instant visual feedback the moment you start speaking,
                    # instead of only after silence + Whisper transcription.
                    await manager.send_state(AgentState.LISTENING, detail="listening…")

        return np.concatenate(collected) if collected else np.empty(0, dtype=np.int16)

    def _match_wake(self, text: str, variants: tuple[str, ...]) -> tuple[bool, str]:
        """If `text` starts with the wake word (optionally prefixed by 'hey'),
        return (True, trailing-command). Command is '' when only the wake word
        was said."""
        norm = self._normalize(text)
        if norm.startswith("hey "):
            norm = norm[4:]
        for v in variants:
            if norm == v or norm.startswith(v + " "):
                return True, norm[len(v):].strip(" ,.-:;")
        return False, ""

    async def listen(self, mic_stream: AsyncIterator[np.ndarray]) -> AsyncIterator[str]:
        from voice.stt import transcribe  # lazy: avoids import cost when unused
        from voice.audio_loop import drain_mic  # late import: avoids a circular import

        wake_variants = self._phrases(settings.wake_word_on)
        sleep_variants = self._phrases(settings.wake_word_off)

        while True:
            audio = await self._collect_utterance(mic_stream)
            if audio.size == 0:
                continue
            rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
            if rms < self.MIN_RMS:
                continue
            text = (await transcribe(audio) or "").strip()
            # Whisper just held this loop for ~1s while the mic kept recording.
            # Without dropping that backlog, the next _collect_utterance() reads
            # audio from the past -- and since every turn adds another second of
            # arrears, the assistant falls further behind the longer you use it.
            drain_mic()
            if not text:
                continue
            logger.debug("wake heard: {!r}", text[:100])

            matched, command = self._match_wake(text, wake_variants)
            if matched:
                self.pending_command = command
                logger.info("Wake word detected (command={!r})", command[:80])
                yield "wake_up"
                return

            norm = self._normalize(text)
            if any(v in norm for v in sleep_variants):
                yield "go_to_sleep"
                return

            # Speech wasn't addressed to us -- drop the "listening" pulse back
            # to sleeping so the avatar doesn't stay lit on background chatter.
            await manager.send_state(AgentState.SLEEPING, detail="waiting for wake word")


def make_wake_detector():
    """Returns the configured wake detector (openwakeword or whisper-based)."""
    if settings.wake_engine.lower() == "whisper":
        return WhisperWakeDetector()
    return WakeWordDetector()
