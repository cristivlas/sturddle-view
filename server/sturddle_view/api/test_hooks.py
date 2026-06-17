"""Test-only endpoints for installing HVE state and reading invariants.

Mounted only when ``Settings.test_mode=True``. Never enabled in
production. Used by the e2e subprocess fixture to seed scenarios that
would otherwise require deep access to ``app.state.hve`` internals.
"""
from __future__ import annotations

import logging
from typing import Any

import chess
from fastapi import APIRouter, HTTPException, Request

from ..events import Event
from ..play.chess_clock import ChessClock, TimeControl
from ..play.human_vs_engine import HumanVsEngine, ViewModeParams

router = APIRouter(prefix="/_test")
log = logging.getLogger(__name__)


@router.post("/hve/install")
async def install_hve(payload: dict, request: Request) -> dict:
    """Install a synthesized HVE on ``app.state.hve``.

    Body fields (all optional unless noted):
      * ``engine_path`` -- defaults to ``"/nonexistent"`` (no real engine).
      * ``engine_name`` -- optional display name.
      * ``human_white`` (bool, default True).
      * ``moves_uci`` (list[str]) -- pushed onto the board in order.
      * ``tc`` -- ``{"initial_seconds": float, "increment_seconds": float}``.
      * ``white_time`` / ``black_time`` -- direct overrides post-construction.
      * ``view_mode`` (bool, default False) -- enter view mode after
        installing. If True, the HVE is set up via ``enter_view_mode``
        rather than as a live play game.
      * ``view_start_fen`` -- start FEN for view mode.
      * ``view_moves_uci`` -- moves for view mode (alternative to top-level).
      * ``game_id`` -- override game id (default ``"test-game"``).
    """
    app = request.app
    engine_path = payload.get("engine_path", "/nonexistent")
    hve = HumanVsEngine(
        engine_path=engine_path,
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    if payload.get("engine_name"):
        hve._engine_name = payload["engine_name"]
    view_mode = bool(payload.get("view_mode", False))
    if view_mode:
        await hve.enter_view_mode(ViewModeParams(
            start_fen=payload.get("view_start_fen"),
            moves_uci=payload.get("view_moves_uci", []),
            clock_history=payload.get("view_clock_history"),
        ))
    else:
        hve._board = chess.Board(payload["start_fen"]) if payload.get("start_fen") else chess.Board()
        for uci in payload.get("moves_uci", []):
            hve._board.push_uci(uci)
        hve._eval_history = [None] * len(hve._board.move_stack)
        hve._human_white = bool(payload.get("human_white", True))
        tc_spec = payload.get("tc", {})
        tc = TimeControl(
            initial_seconds=float(tc_spec.get("initial_seconds", 300.0)),
            increment_seconds=float(tc_spec.get("increment_seconds", 0.0)),
        )
        hve._clock = ChessClock(tc)
        if "white_time" in payload:
            hve._clock.white_time = float(payload["white_time"])
        if "black_time" in payload:
            hve._clock.black_time = float(payload["black_time"])
        hve._game_id = payload.get("game_id", "test-game")
        hve._clock.start_turn()
    app.state.hve = hve
    return {
        "game_id": hve._game_id,
        "board_fen": hve._board.fen() if hve._board is not None else None,
        "viewing": view_mode,
    }


@router.get("/tournament/proxy_secret")
def tournament_proxy_secret(request: Request) -> dict[str, str | None]:
    """Return the active tournament's proxy secret (or None)."""
    orch = request.app.state.tournament_orch
    return {"secret": orch.proxy_secret()}


@router.post("/ai/publish_event")
async def publish_ai_event(payload: dict, request: Request) -> dict:
    """Publish a synthetic AI event onto the event bus.

    Body: ``{"kind": str, "game_id": str|None, "payload": dict}``.
    Used by e2e tests to drive ai_tool_call / ai_tool_call_complete
    without booting a real LLM agent loop -- the client-side preview
    wiring reacts to the bus event the same way regardless."""
    kind = payload.get("kind")
    if not isinstance(kind, str) or not kind:
        raise HTTPException(status_code=400, detail="kind required")
    await request.app.state.event_bus.publish(Event(
        kind=kind,
        game_id=payload.get("game_id"),
        payload=payload.get("payload") or {},
    ))
    return {"ok": True}


@router.post("/ai/seed_replay")
async def seed_ai_replay(payload: dict, request: Request) -> dict:
    """Prime the AI coordinator's replay buffer with envelope-shaped events.

    A subsequent page load rehydrates the AI panel from these exactly as a
    client reconnecting after a turn would (GET /game/analysis/replay). Lets
    e2e tests exercise the replay-render path without a real LLM turn.

    Body: ``{"events": [{"kind", "payload", "game_id"}]}``."""
    coord = getattr(request.app.state, "ai_coordinator", None)
    if coord is None:
        raise HTTPException(status_code=400, detail="no ai coordinator")
    events = payload.get("events") or []
    coord.seed_replay(events)
    return {"ok": True, "count": len(events)}


@router.post("/recents/seed_fork")
async def seed_fork(payload: dict, request: Request) -> dict:
    """Seed a parent + child recents pair with a fork link via the real
    ``RecentImports.save`` (same path the finalizer uses). Importing the
    child's text later reuses the stored game_id (first-save-wins), so
    ``by-id`` serves the fork for a real view-mode game -- no engine.

    Body: ``{"parent": {game_id, text, summary},
             "child": {game_id, text, summary, fork_ply}}``."""
    recents = request.app.state.recent_imports
    parent = payload["parent"]
    child = payload["child"]
    await recents.save(
        fmt="pgn", text=parent["text"], summary=parent["summary"],
        game_id=parent["game_id"],
    )
    await recents.save(
        fmt="pgn", text=child["text"], summary=child["summary"],
        game_id=child["game_id"],
        parent_game_id=parent["game_id"], fork_ply=child["fork_ply"],
    )
    return {
        "parent_game_id": parent["game_id"],
        "child_game_id": child["game_id"],
        "fork_ply": child["fork_ply"],
    }


@router.get("/hve/state")
def hve_state(request: Request) -> dict[str, Any]:
    """Read invariants tests assert on. Returns ``hve is None`` if no HVE."""
    hve = request.app.state.hve
    if hve is None:
        return {"hve": None}
    return {
        "hve": True,
        "game_id": hve._game_id,
        "view_cursor": hve._view_cursor,
        "viewing": hve._viewing,
        "human_white": hve._human_white,
        "player_name": hve._player_name,
        "paused": hve._paused,
        "board_fen": hve._board.fen() if hve._board is not None else None,
        "move_stack_uci": [m.uci() for m in hve._board.move_stack] if hve._board is not None else [],
        "n_plies": len(hve._board.move_stack) if hve._board is not None else 0,
    }
