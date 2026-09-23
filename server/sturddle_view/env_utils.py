"""Shared env-var parsing: a malformed value logs a warning and falls
back to the default."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_NON_NUMERIC_WARNING = "ignoring non-numeric %s=%r; using default %s"


def env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning(_NON_NUMERIC_WARNING, name, raw, default)
        return default
    if min_value is not None and value < min_value:
        log.warning("ignoring %s=%r below %s; using default %s", name, raw, min_value, default)
        return default
    return value


_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off", ""}


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    log.warning("ignoring non-boolean %s=%r; using default %s", name, raw, default)
    return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning(_NON_NUMERIC_WARNING, name, raw, default)
        return default
