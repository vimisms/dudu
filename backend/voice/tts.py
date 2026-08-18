"""Offline text-to-speech via Piper. Returns a complete WAV byte-string per
utterance, which ws_manager base64-encodes and streams to the frontend for
playback (kept simple; for very long replies you could chunk by sentence and
stream multiple 'audio' events instead)."""
from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path

from loguru import logger

from config import settings

_voice = None


def _lazy_load():
    global _voice
    if _voice is None:
        from piper import PiperVoice

        model_path = Path(settings.piper_voice_path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"Piper voice model not found at {model_path}. Download one from "
                "https://github.com/rhasspy/piper/releases (see docs/SETUP.md)."
            )
        logger.info("Loading Piper voice '{}'...", model_path.name)
        _voice = PiperVoice.load(str(model_path))
    return _voice


async def synthesize(text: str) -> bytes:
    """Returns a mono 16-bit PCM WAV file as bytes."""
    voice = _lazy_load()

    def _run() -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        return buf.getvalue()

    wav_bytes = await asyncio.to_thread(_run)
    logger.debug("TTS synthesized {} bytes for {!r}", len(wav_bytes), text[:60])
    return wav_bytes
