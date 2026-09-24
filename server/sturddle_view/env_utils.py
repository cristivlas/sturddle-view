"""Shared env-var parsing: a malformed or out-of-range value logs a warning
and falls back to the default."""
from __future__ import annotations

import logging
import os
from collections.abc import Callable, Collection
from pathlib import Path
from typing import TypeVar

log = logging.getLogger(__name__)

_NON_NUMERIC_WARNING = "ignoring non-numeric %s=%r; using default %s"
_BELOW_MIN_WARNING = "ignoring %s=%r below %s; using default %s"
_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off", ""}

_Number = TypeVar("_Number", int, float)


def _env_number(
    name: str, default: _Number, parse: Callable[[str], _Number], min_value: _Number | None,
) -> _Number:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = parse(raw)
    except ValueError:
        log.warning(_NON_NUMERIC_WARNING, name, raw, default)
        return default
    if min_value is not None and value < min_value:
        log.warning(_BELOW_MIN_WARNING, name, raw, min_value, default)
        return default
    return value


def env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    return _env_number(name, default, int, min_value)


def env_float(name: str, default: float, *, min_value: float | None = None) -> float:
    return _env_number(name, default, float, min_value)


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


def env_choice(name: str, default: str, choices: Collection[str]) -> str:
    """Case-insensitive pick from ``choices`` (given in lower case)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in choices:
        return token
    log.warning("ignoring %s=%r (expected one of %s); using default %s",
                name, raw, sorted(choices), default)
    return default


def env_path(name: str, default: Path) -> Path:
    """The env var as a Path when set and non-empty, else ``default``."""
    raw = os.environ.get(name)
    return Path(raw) if raw else default
