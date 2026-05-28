"""AI analysis turn kickoff + cancel, called from `/game/analysis/*`.

The client never talks to AI directly: it asks the server to start or
stop analysis, and the server picks the path (engine go-infinite or
AI agent) based on `settings.ai_enabled`. This module owns the AI
half of that choice -- bridging the coordinator, provider factory,
and HVE board snapshot at the API boundary.

Reads HVE state via its accessors (current_board / start_fen /
view_full_moves_san / pre_analysis_mode). Lives at the API boundary
to keep the coordinator free of HTTP/HVE plumbing.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request

from ..chess.board import moves_san
from ..llm import PromptMode, build_initial_user_message
from ..llm.ollama import DEFAULT_BASE_URL as _DEFAULT_OLLAMA_BASE_URL, OllamaProvider
from ..play.mode import Mode

log = logging.getLogger(__name__)


def _san_history_for(hve) -> list[str]:
    """Pick the right move list to ship to the agent.

    View mode: the FULL game's moves. The current cursor position is
    carried by the FEN; everything past that ply is "future" the
    commentator can reference.

    Play mode: the live board's move_stack -- there is no future.
    """
    full = hve.view_full_moves_san()
    if full:
        return full
    return moves_san(hve.current_board(), hve.start_fen())


def _short_engine_name(full: str | None) -> str | None:
    # First whitespace token only: full UCI ids ("MyEngine 2.5.1-rc9.050226")
    # otherwise leak verbatim into model prose.
    if not full:
        return full
    return full.split()[0] or full


def _build_user_message(hve) -> str | None:
    if hve is None:
        return None
    board = hve.current_board()
    if board is None:
        return None
    opening = hve.lookup_opening()
    # `*` means "result unknown / unfinished" in PGN; treat as absent.
    raw_result = hve.viewed_pgn_result()
    result = raw_result if raw_result and raw_result != "*" else None
    san_history = _san_history_for(hve)
    # View mode: SAN at index ply is the move actually played from
    # the position-under-review. Lets the commentator distinguish the
    # played move from alternatives it explored via tools.
    ply = len(board.move_stack)
    move_played = san_history[ply] if ply < len(san_history) else None
    return build_initial_user_message(
        fen=board.fen(),
        san_history=san_history,
        engine_name=_short_engine_name(hve.engine_display_name()),
        opening_eco=opening.eco if opening else None,
        opening_name=opening.name if opening else None,
        result=result,
        move_played=move_played,
    )


def _prompt_mode_for(hve) -> PromptMode:
    """Pick the persona from where analysis was entered.

    - View mode (replaying a PGN) -> commentator: third-person, can
      reference what happens later.
    - Anywhere else (live play, paused) -> coach: second-person.
    """
    if hve is None:
        return "coach"
    if hve.pre_analysis_mode() is Mode.VIEWING:
        return "commentator"
    return "coach"


async def _evict_stale_ollama_models(base_url: str, target_model: str) -> None:
    """Ask the daemon (/api/ps) what's loaded and evict anything other
    than `target_model`. All failures swallowed: worst case is the
    daemon's own "resource limits" error on the next load."""
    if not target_model:
        log.debug("ollama: no target model selected; skipping stale-model evict")
        return
    provider = OllamaProvider(base_url=base_url, model="")
    try:
        loaded = await provider.list_loaded_models()
    except Exception as exc:
        log.warning("ollama: list_loaded_models failed: %s", exc)
        return
    for name in loaded:
        if name == target_model:
            continue
        try:
            await provider.evict_model(name)
            log.info("ollama: evicted stale model %s", name)
        except Exception as exc:
            log.warning("ollama: evict_model(%s) failed: %s", name, exc)


def _consume_task_exception(task: asyncio.Task) -> None:
    # Read the result so asyncio doesn't warn about an unretrieved
    # exception. The coordinator already logs + publishes a done event
    # with error/error_detail for any failure it sees; this callback is
    # just here so the GC doesn't shout.
    if task.cancelled():
        return
    task.exception()


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
    s = request.app.state.settings
    if (s.ai_provider or "").lower() == "ollama":
        base_url = s.ai_base_url or _DEFAULT_OLLAMA_BASE_URL
        await _evict_stale_ollama_models(base_url, s.ai_model or "")
    hve = request.app.state.hve
    game_id = getattr(hve, "game_id", None) if hve else None
    user_message = _build_user_message(hve)
    mode = _prompt_mode_for(hve)
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
        coord.run(
            game_id=game_id,
            provider=provider,
            user_message=user_message,
            mode=mode,
        )
    )
    task.add_done_callback(_consume_task_exception)
    request.app.state.ai_task = task


async def cancel_ai_turn(request: Request) -> None:
    """Cancel any in-flight AI turn. No-op if nothing is running."""
    coord = getattr(request.app.state, "ai_coordinator", None)
    if coord is None:
        return
    await coord.cancel()
