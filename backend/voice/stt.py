"""Speech-to-text via faster-whisper (CTranslate2 Whisper build -- fast on CPU,
which matters here since this runs continuously alongside wake-word detection)."""
from __future__ import annotations

import asyncio
import time

import numpy as np
from loguru import logger

from config import settings

_model = None


def _lazy_load():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        logger.info("Loading Whisper model '{}'...", settings.whisper_model_size)
        _model = WhisperModel(settings.whisper_model_size, device="cpu", compute_type="int8")
    return _model


async def transcribe(audio_i16: np.ndarray, sample_rate: int = 16000) -> str:
    """audio_i16: mono int16 PCM samples. Returns the transcribed text (stripped)."""
    model = _lazy_load()
    audio_f32 = audio_i16.astype(np.float32) / 32768.0
    audio_seconds = len(audio_i16) / float(sample_rate)

    def _run() -> str:
        # vad_filter was True here, but every caller has ALREADY gated this
        # audio through webrtcvad -- so it ran Silero VAD a second time over
        # known-speech audio, adding latency to the critical path (and
        # occasionally clipping a leading word). beam_size=1 (greedy) is the
        # other lever: the default beam search costs roughly 2x for accuracy
        # that doesn't matter on short command phrases.
        segments, _info = model.transcribe(
            audio_f32,
            language="en",
            vad_filter=False,
            beam_size=1,
            condition_on_previous_text=False,  # each utterance is independent
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    t0 = time.monotonic()
    text = await asyncio.to_thread(_run)
    elapsed = time.monotonic() - t0
    # Real-time factor: <1.0 means transcription is faster than the speech it
    # transcribed. If this approaches 1.0 the model is too big for this machine
    # -- drop WHISPER_MODEL_SIZE to "base.en" or "tiny.en".
    logger.info(
        "STT: {:.1f}s audio in {:.2f}s (RTF {:.2f}) -> {!r}",
        audio_seconds, elapsed, elapsed / max(audio_seconds, 0.01), text[:80],
    )
    return text
