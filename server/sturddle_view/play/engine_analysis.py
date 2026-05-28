"""Shared analysis-engine spawn helpers.

Both the live engine analysis path (HVE._run_analysis) and the AI agent
analysis tool need a throwaway engine configured with the *same*
settings-driven options (Threads, Hash, SyzygyPath, plus the analysis-
specific Threads override). This module owns that config so neither
caller drifts.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

import chess.engine

from .engine_supervisor import EngineSupervisor


# UCI option keys -- kept as constants to avoid scattered string literals
# in two callers + tests.
_UCI_THREADS = "Threads"
_UCI_HASH = "Hash"
_UCI_SYZYGY_PATH = "SyzygyPath"


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
