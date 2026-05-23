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
