"""Application-wide logging configuration.

Single entry point: `configure_logging(level)`. Idempotent -- safe to call
multiple times.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from platformdirs import user_log_dir

from . import app_dir_name


def default_log_dir() -> Path:
    return Path(user_log_dir(app_dir_name(), appauthor=False))


_configured = False


def configure_logging(
    level: int = logging.INFO,
    server_level: int | None = None,
    log_dir: Path | None = None,
) -> Path:
    """``level`` controls the ``sturddle_view`` logger tree.
    ``server_level`` controls ``uvicorn``; defaults to WARNING so
    uvicorn doesn't drown app logs even when --debug is on. Pass
    ``DEBUG`` to debug the server itself."""
    global _configured
    log_dir = log_dir or default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "sturddle-view.log"
    if server_level is None:
        server_level = logging.WARNING

    if _configured:
        logging.getLogger("sturddle_view").setLevel(level)
        logging.getLogger("chess").setLevel(level)
        logging.getLogger("uvicorn").setLevel(server_level)
        logging.getLogger("uvicorn.access").setLevel(max(server_level, logging.WARNING))
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

    # Stream level tracks the *minimum* of the requested levels so
    # nothing the user asked for ends up only in the file.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(min(level, server_level))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # handlers gate visibility
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    logging.getLogger("sturddle_view").setLevel(level)
    # python-chess library: clamp to our app level. Defaults to INFO,
    # so the raw UCI byte trace on `chess.engine` (DEBUG) doesn't
    # flood the file handler. ``--debug`` opens it back up for engine
    # diagnostics.
    logging.getLogger("chess").setLevel(level)
    logging.getLogger("uvicorn").setLevel(server_level)
    # Per-request access lines are noise; clamp at WARNING regardless.
    logging.getLogger("uvicorn.access").setLevel(max(server_level, logging.WARNING))
    # WS lifecycle ("connection open/closed", "[accepted]") is INFO in
    # uvicorn.error -- too chatty. Demote those records to DEBUG so they
    # only land in the file handler, not the console.
    logging.getLogger("uvicorn.error").addFilter(_demote_ws_lifecycle)

    _configured = True
    return log_file


_WS_LIFECYCLE_MARKERS = ("connection open", "connection closed", "WebSocket ")


def _demote_ws_lifecycle(record: logging.LogRecord) -> bool:
    if record.levelno == logging.INFO:
        msg = record.getMessage()
        if any(m in msg for m in _WS_LIFECYCLE_MARKERS):
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
    return True
