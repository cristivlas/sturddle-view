"""Application-wide logging configuration.

Single entry point: `configure_logging(level)`. Idempotent — safe to call
multiple times.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from platformdirs import user_log_dir


def default_log_dir() -> Path:
    return Path(user_log_dir("sturddle-view"))


_configured = False


def configure_logging(level: int = logging.INFO, log_dir: Path | None = None) -> Path:
    global _configured
    log_dir = log_dir or default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "sturddle-view.log"

    if _configured:
        # Update level only.
        logging.getLogger().setLevel(level)
        return log_file

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(min(level, logging.DEBUG))
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # Quiet down noisy uvicorn access logs at INFO; let DEBUG see them.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _configured = True
    return log_file
