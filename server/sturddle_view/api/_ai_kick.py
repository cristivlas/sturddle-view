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
import os
from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..chess.board import moves_san
from ..env_utils import env_int
from ..llm import PromptMode, build_initial_user_message
from ..llm.ollama import DEFAULT_BASE_URL as _DEFAULT_OLLAMA_BASE_URL, OllamaProvider
from ..play.mode import Mode
from ..play.opening_reply import book_ref_from_settings, probe_opening_reply

log = logging.getLogger(__name__)


# Per-comment + total annotation budgets (chars). Caps prompt size.
# Raise via env for larger-context models.
_PER_COMMENT_MAX_DEFAULT = 200
_TOTAL_COMMENT_MAX_DEFAULT = 1500
_PER_COMMENT_MAX_ENV = "SV_AI_ANNOTATION_PER_COMMENT_MAX"
_TOTAL_COMMENT_MAX_ENV = "SV_AI_ANNOTATION_TOTAL_MAX"
# Marker appended to a comment that was truncated mid-string.
_TRUNCATION_MARKER = "..."

# Plies past the matched opening line that still count as "in the opening"
# for the opening-theory directive; beyond it the game has left book and the
# steer is suppressed. Raise via env for slower book-to-middlegame handoffs;
# 0 means "only while exactly on the named line".
_OPENING_PHASE_SLACK_DEFAULT = 12
_OPENING_PHASE_SLACK_ENV = "SV_AI_OPENING_PHASE_SLACK_PLIES"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def _cap_annotations(
    comments: list[str | None] | None,
    root_comment: str | None,
    per_comment_max: int,
    total_max: int,
) -> tuple[list[str | None] | None, str | None]:
    """Truncate each comment to `per_comment_max` chars (with marker),
    then drop trailing entries once the running total exceeds `total_max`.
    Returns (None, None) when nothing survives. Root comment is capped
    independently and counts against the total before per-ply entries.
    """
    capped_root: str | None = None
    budget = total_max
    if root_comment:
        capped_root = _truncate(root_comment, per_comment_max)
        budget -= len(capped_root)
    capped: list[str | None] | None = None
    if comments:
        capped = []
        for c in comments:
            if c is None:
                capped.append(None)
                continue
            if budget <= 0:
                capped.append(None)
                continue
            piece = _truncate(c, min(per_comment_max, budget))
            capped.append(piece)
            budget -= len(piece)
        if not any(p is not None for p in capped):
            capped = None
    return capped, capped_root


def _truncate(s: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(s) <= limit:
        return s
    # Marker would overflow the cap; drop it and hard-cut.
    if limit < len(_TRUNCATION_MARKER):
        return s[:limit]
    return s[:limit - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


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


_COMMENTATOR_MODE: PromptMode = "commentator"
_COACH_MODE: PromptMode = "coach"


def _prompt_mode_for(hve) -> PromptMode:
    """Pick the persona from where analysis was entered.

    - View mode (replaying a PGN) -> commentator: third-person, can
      reference what happens later.
    - Anywhere else (live play, paused) -> coach: second-person.
    """
    if hve is None:
        return _COACH_MODE
    if hve.pre_analysis_mode() is Mode.VIEWING:
        return _COMMENTATOR_MODE
    return _COACH_MODE


@dataclass(slots=True, frozen=True)
class TurnInputs:
    """What one AI turn is kicked with: the opening user message, the
    book move (UCI) the loop accepts without a red-team hold, if any,
    and its sibling theory moves for the board's secondary arrows."""

    user_message: str
    book_move_uci: str | None
    book_alternatives: tuple[str, ...]


async def _build_turn_inputs(hve, settings, eco_book) -> TurnInputs | None:
    """Build the user message (and book move, if any) for one turn from a
    single SAN walk."""
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
    # SAN at the current ply is the move played from the position under
    # review (view mode); None in play mode where there is no future.
    ply = len(board.move_stack)
    move_played = san_history[ply] if ply < len(san_history) else None
    mode = _prompt_mode_for(hve)
    annotations: list[str | None] | None = None
    root_annotation: str | None = None
    # Gate on the prompt persona, not Mode.VIEWING: keeps annotation
    # plumbing aligned with the commentator addendum that tells the
    # model how to weigh them.
    if mode == _COMMENTATOR_MODE:
        raw_comments, raw_root = hve.view_game_comments()
        per_max = _int_env(_PER_COMMENT_MAX_ENV, _PER_COMMENT_MAX_DEFAULT)
        total_max = _int_env(_TOTAL_COMMENT_MAX_ENV, _TOTAL_COMMENT_MAX_DEFAULT)
        annotations, root_annotation = _cap_annotations(
            raw_comments, raw_root, per_max, total_max,
        )
    # Both personas: when the position is still in (or just past) the named
    # opening line, trigger the opening-theory directive (a related_openings
    # call + one grounded sentence). Coach teaches the plan as it is played;
    # commentator contrasts variations after the fact.
    in_opening = opening is not None and ply <= opening.ply + env_int(
        _OPENING_PHASE_SLACK_ENV, _OPENING_PHASE_SLACK_DEFAULT
    )
    # Theory fast path (see BOOK_REPLY_GUIDANCE): the reply rides the
    # message and clears the loop's red-team hold. Off-loop: the book's
    # first index build is slow. The armed ref's anchor is the game-start
    # cursor, so it names the engine's own line. View mode: the move
    # played here is the reply whenever theory knows it.
    reply = None
    if hve.start_fen() is None:
        reply = await asyncio.to_thread(
            probe_opening_reply,
            eco_book,
            hve.book_ref() or book_ref_from_settings(settings),
            board.copy(),
            opening,
            board.parse_san(move_played).uci() if move_played else None,
        )
    else:
        log.info("opening reply: probe skipped (custom start FEN)")
    message = build_initial_user_message(
        fen=board.fen(),
        san_history=san_history,
        engine_name=_short_engine_name(hve.engine_display_name()),
        opening_eco=opening.eco if opening else None,
        opening_name=opening.name if opening else None,
        result=result,
        move_played=move_played,
        annotations=annotations,
        root_annotation=root_annotation,
        in_opening=in_opening,
        book_reply=reply,
    )
    if reply is None:
        return TurnInputs(message, None, ())
    return TurnInputs(message, reply.uci, tuple(a.uci for a in reply.alternatives))


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


def _on_turn_done(task: asyncio.Task, state) -> None:
    # Read the result so asyncio doesn't warn about an unretrieved
    # exception. The coordinator already logs + publishes a done event
    # with error/error_detail for any failure it sees.
    if task.cancelled():
        return
    if task.exception() is None:
        return
    # Identity guard: a later turn may have started after this one errored
    # (user re-Analyzed). Only the still-current task may exit ANALYZING --
    # else this stale callback tears down the live turn's mode.
    if task is not getattr(state, "ai_task", None):
        return
    hve = getattr(state, "hve", None)
    if hve is None:
        return
    # A turn that died never produced analysis: exit ANALYZING so the
    # server doesn't sit in a mode with no turn running. The callback is
    # sync, so schedule it; pin the task on state (same GC hazard as the
    # turn task -- an unreferenced task can be reaped mid-flight).
    state.ai_stop_task = asyncio.ensure_future(hve.stop_analysis())


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
    inputs = await _build_turn_inputs(
        hve, s, getattr(request.app.state, "openings", None)
    )
    mode = _prompt_mode_for(hve)
    # Latest-start-wins: hard-stop any in-flight turn first. run() holds a
    # single lock for the whole turn, so without this the new turn queues
    # behind the old one instead of superseding it. Idempotent; no-op if idle.
    await coord.cancel()
    # Build the provider per turn so settings changes (model, base URL,
    # API key) flow through without a coordinator rebuild. Not in the
    # hot path -- happens once per AI turn.
    try:
        provider = factory()
    except Exception as e:
        # Surfaced to the client as HTTP 500 with the message; the stack
        # is noise (config/build failure, not an internal bug).
        log.error("AI provider factory failed: %s", e)
        raise HTTPException(status_code=500, detail=f"AI provider error: {e}") from e
    # Pin the task on app.state so the event loop holds a strong ref --
    # asyncio GC can otherwise reap an unreferenced task mid-flight.
    task = asyncio.create_task(
        coord.run(
            game_id=game_id,
            provider=provider,
            user_message=inputs.user_message if inputs else None,
            book_move_uci=inputs.book_move_uci if inputs else None,
            book_alternatives=inputs.book_alternatives if inputs else (),
            mode=mode,
            max_tool_rounds=s.ai_max_tool_rounds,
            verifier_max_rounds=s.ai_verifier_max_rounds,
        )
    )
    state = request.app.state
    state.ai_task = task
    task.add_done_callback(lambda t: _on_turn_done(t, state))


async def cancel_ai_turn(request: Request) -> None:
    """Cancel any in-flight AI turn. No-op if nothing is running."""
    coord = getattr(request.app.state, "ai_coordinator", None)
    if coord is None:
        return
    await coord.cancel()
