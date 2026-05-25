"""AI analysis turn kickoff + cancel, called from `/game/analysis/*`.

The client never talks to AI directly: it asks the server to start or
stop analysis, and the server picks the path (engine go-infinite or
AI agent) based on `settings.ai_enabled`. This module owns the AI
half of that choice -- bridging the coordinator, provider factory,
and HVE board snapshot at the API boundary.

Direct hve._board / _start_fen access mirrors api/game.py's existing
shortcut; replace when the coordinator owns its own session state.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request

from ..chess.board import moves_san
from ..llm import build_initial_user_message

log = logging.getLogger(__name__)


def _build_user_message(hve) -> str | None:
    if hve is None:
        return None
    board = getattr(hve, "_board", None)
    if board is None:
        return None
    start_fen = getattr(hve, "_start_fen", None)
    return build_initial_user_message(
        fen=board.fen(),
        san_history=moves_san(board, start_fen),
    )


def _log_task_exception(task: asyncio.Task) -> None:
    # Surface unhandled errors from the fire-and-forget run() task --
    # otherwise they vanish silently when the task is GC'd.
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.exception("AI coordinator run failed", exc_info=exc)


async def start_ai_turn(request: Request) -> None:
    """Kick one AI analysis turn. Idempotent w.r.t. start_analysis --
    callers should invoke this only when `settings.ai_enabled` is true.

    Raises HTTPException on configuration/provider errors so the
    /game/analysis/start endpoint can surface them as 4xx/5xx.
    """
    coord = getattr(request.app.state, "ai_coordinator", None)
    if coord is None:
        raise HTTPException(status_code=503, detail="AI coordinator not initialized")
    factory = getattr(request.app.state, "ai_provider_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="AI provider factory not initialized")
    hve = request.app.state.hve
    game_id = getattr(hve, "game_id", None) if hve else None
    user_message = _build_user_message(hve)
    # Build the provider per turn so settings changes (model, base URL,
    # API key) flow through without a coordinator rebuild. Not in the
    # hot path -- happens once per AI turn.
    try:
        provider = factory()
    except Exception as e:
        log.exception("AI provider factory failed")
        raise HTTPException(status_code=500, detail=f"AI provider error: {e}") from e
    # Pin the task on app.state so the event loop holds a strong ref --
    # asyncio GC can otherwise reap an unreferenced task mid-flight.
    task = asyncio.create_task(
        coord.run(game_id=game_id, provider=provider, user_message=user_message)
    )
    task.add_done_callback(_log_task_exception)
    request.app.state.ai_task = task


async def cancel_ai_turn(request: Request) -> None:
    """Cancel any in-flight AI turn. No-op if nothing is running."""
    coord = getattr(request.app.state, "ai_coordinator", None)
    if coord is None:
        return
    await coord.cancel()
