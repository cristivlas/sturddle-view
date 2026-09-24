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

from ..config import (
    ENGINE_ANALYSIS_THREADS_KEY,
    ENGINE_HASH_MB_KEY,
    ENGINE_SYZYGY_PATH_KEY,
    ENGINE_THREADS_KEY,
    EVAL_POV_ENGINE,
    EVAL_POV_HUMAN,
    EVAL_POV_WHITE,
    PLAY_EVAL_POV_KEY,
)
from ..engines import UCI_OPT_HASH, UCI_OPT_SYZYGY_PATH, UCI_OPT_THREADS, resolve_analysis
from .engine_supervisor import EngineSupervisor

log = logging.getLogger(__name__)


class NoAnalysisEngine(RuntimeError):
    """No analysis engine is configured/resolvable -- a config gap, not a crash."""


def log_spawn_failure(exc: Exception, context: str) -> None:
    """Log an analysis-engine spawn failure. A missing binary or unconfigured
    engine is expected and user-fixable -- log it concisely; anything else is a
    real surprise, so keep the full traceback."""
    if isinstance(exc, FileNotFoundError):
        log.warning("%s: engine not found: %s", context, exc)
    elif isinstance(exc, NoAnalysisEngine):
        log.warning("%s: %s", context, exc)
    else:
        log.error("%s: engine spawn failed", context, exc_info=True)


def eval_pov_mode(settings: Any | None) -> str:
    """The play_eval_pov setting; white when there are no settings."""
    return getattr(settings, PLAY_EVAL_POV_KEY, EVAL_POV_WHITE) if settings else EVAL_POV_WHITE


def resolve_eval_pov_white_or_stm(
    settings: Any | None, stm: chess.Color,
) -> chess.Color:
    """Resolve play_eval_pov to a serialization POV for engine_info
    events. Handles the modes that don't need human/engine assignment
    context: 'white' -> white; 'engine' -> STM. 'human' falls back to
    STM (caller-specific handling lives in HVE; tools have no human
    color)."""
    if eval_pov_mode(settings) in (EVAL_POV_ENGINE, EVAL_POV_HUMAN):
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
    threads = getattr(settings, ENGINE_THREADS_KEY, None)
    if threads:
        out[UCI_OPT_THREADS] = threads
    hash_mb = getattr(settings, ENGINE_HASH_MB_KEY, None)
    if hash_mb:
        out[UCI_OPT_HASH] = hash_mb
    syzygy_path = getattr(settings, ENGINE_SYZYGY_PATH_KEY, None)
    if syzygy_path:
        out[UCI_OPT_SYZYGY_PATH] = syzygy_path
    return out


def analysis_overrides(settings: Any | None) -> dict:
    """Per-call overrides applied to analysis spawns on top of the
    global defaults. The analysis-Threads count is typically higher
    than play-Threads (analysis is bursty and benefits from more
    cores)."""
    if settings is None:
        return {}
    n = getattr(settings, ENGINE_ANALYSIS_THREADS_KEY, None)
    return {UCI_OPT_THREADS: n} if n else {}


def make_analysis_supervisor(registry, settings: Any | None, bus) -> EngineSupervisor:
    """Build a supervisor for the configured analysis engine. resolve_analysis
    honors analysis_engine_id (falling back to the active engine only when the
    pinned entry is gone). Shared by HVE engine-only analysis and the AI tool
    so neither drifts onto the wrong engine."""
    launch = resolve_analysis(registry, settings) if registry is not None else None
    if launch is None or launch.path is None:
        raise NoAnalysisEngine("no analysis engine configured")
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
