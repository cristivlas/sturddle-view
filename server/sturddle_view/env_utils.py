"""Shared env-var helpers for the tournament subsystem."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("ignoring non-numeric %s=%r; using default %s", name, raw, default)
        return default


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
        log.warning("ignoring non-numeric %s=%r; using default %s", name, raw, default)
        return default
