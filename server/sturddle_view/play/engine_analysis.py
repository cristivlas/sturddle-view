"""Shared analysis-engine spawn helpers.

Both the live engine analysis path (HVE._run_analysis) and the AI agent
analysis tool need a throwaway engine configured with the *same*
settings-driven options (Threads, Hash, SyzygyPath, plus the analysis-
specific Threads override). This module owns that config so neither
caller drifts.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import chess
import chess.engine

from ..engines import resolve_analysis
from .engine_supervisor import EngineSupervisor

log = logging.getLogger(__name__)


def log_spawn_failure(exc: Exception, context: str) -> None:
    """Log an analysis-engine spawn failure. A missing binary (stale engine
    path) is expected and user-fixable -- log it concisely; anything else is a
    real surprise, so keep the full traceback."""
    if isinstance(exc, FileNotFoundError):
        log.warning("%s: engine not found: %s", context, exc)
    else:
        log.error("%s: engine spawn failed", context, exc_info=True)


# UCI option keys -- kept as constants to avoid scattered string literals
# in two callers + tests.
_UCI_THREADS = "Threads"
_UCI_HASH = "Hash"
_UCI_SYZYGY_PATH = "SyzygyPath"

# play_eval_pov setting values. Public so callers can branch on them
# without repeating literals.
EVAL_POV_WHITE = "white"
EVAL_POV_ENGINE = "engine"
EVAL_POV_HUMAN = "human"


def resolve_eval_pov_white_or_stm(
    settings: Any | None, stm: chess.Color,
) -> chess.Color:
    """Resolve play_eval_pov to a serialization POV for engine_info
    events. Handles the modes that don't need human/engine assignment
    context: 'white' -> white; 'engine' -> STM. 'human' falls back to
    STM (caller-specific handling lives in HVE; tools have no human
    color)."""
    mode = getattr(settings, "play_eval_pov", EVAL_POV_WHITE) if settings else EVAL_POV_WHITE
    if mode == EVAL_POV_ENGINE or mode == EVAL_POV_HUMAN:
        return stm
    return chess.WHITE


def global_engine_defaults(settings: Any | None) -> dict:
    """Settings-derived UCI option defaults common to every engine
    spawn. Blank/None entries are dropped so callers can iterate
    without an extra guard. Book file + plies are fastchess-only and
    excluded by design."""
    if settings is None:
        return {}
    out: dict = {}
    threads = getattr(settings, "engine_default_threads", None)
    if threads:
        out[_UCI_THREADS] = threads
    hash_mb = getattr(settings, "engine_default_hash_mb", None)
    if hash_mb:
        out[_UCI_HASH] = hash_mb
    syzygy_path = getattr(settings, "engine_default_syzygy_path", None)
    if syzygy_path:
        out[_UCI_SYZYGY_PATH] = syzygy_path
    return out


def analysis_overrides(settings: Any | None) -> dict:
    """Per-call overrides applied to analysis spawns on top of the
    global defaults. The analysis-Threads count is typically higher
    than play-Threads (analysis is bursty and benefits from more
    cores)."""
    if settings is None:
        return {}
    n = getattr(settings, "engine_default_analysis_threads", None)
    return {_UCI_THREADS: n} if n else {}


def make_analysis_supervisor(registry, settings: Any | None, bus) -> EngineSupervisor:
    """Build a supervisor for the configured analysis engine. resolve_analysis
    honors analysis_engine_id (falling back to the active engine only when the
    pinned entry is gone). Shared by HVE engine-only analysis and the AI tool
    so neither drifts onto the wrong engine."""
    launch = resolve_analysis(registry, settings)
    sup = EngineSupervisor(launch.path, bus, settings=settings)
    if launch.options:
        sup.options = launch.options
    if launch.args:
        sup.args = list(launch.args)
    if launch.env:
        sup.env = dict(launch.env)
    return sup


async def spawn_analysis_engine(
    supervisor: EngineSupervisor,
    settings: Any | None,
) -> tuple[chess.engine.UciProtocol, Callable[[], Awaitable[None]]]:
    """Spawn a throwaway analysis engine with the shared settings-
    derived options applied. Returns ``(engine, cleanup)`` -- caller
    MUST await ``cleanup()`` to avoid leaking the asyncio subprocess
    transport."""
    return await supervisor.spawn_throwaway(
        overrides=analysis_overrides(settings),
        global_defaults=global_engine_defaults(settings),
    )
