"""Loguru configuration: readable console output plus a rotating file sink.

Without this, everything the backend logs lives only in whatever terminal
happened to start it -- which is useless the moment the launcher runs the
backend in its own window and you close it. The file sink gives you a
scrollable record of wake-word scores, MCP load failures and task errors.
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

BACKEND_DIR = Path(__file__).resolve().parent

_configured = False


def setup_logging() -> Path | None:
    """Idempotently configure loguru. Returns the log file path, if any."""
    global _configured
    if _configured:
        return None
    _configured = True

    from config import settings  # noqa: PLC0415 - avoid import cycle at module load

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level.upper(),
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
        "<cyan>{name}</cyan> - <level>{message}</level>",
        backtrace=False,
        diagnose=False,
    )

    if not settings.log_file:
        return None

    log_path = Path(settings.log_file)
    if not log_path.is_absolute():
        log_path = BACKEND_DIR / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger.add(
        log_path,
        level=settings.log_level.upper(),
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
        enqueue=True,  # safe to write from the sounddevice/indexer threads too
        backtrace=True,
        diagnose=False,  # never dump local variables -- they can hold API keys
    )
    logger.info("Logging to {}", log_path)
    return log_path
