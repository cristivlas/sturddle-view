"""Test-only endpoints for installing HVE state and reading invariants.

Mounted only when ``Settings.test_mode=True``. Never enabled in
production. Used by the e2e subprocess fixture to seed scenarios that
would otherwise require deep access to ``app.state.hve`` internals.
"""
from __future__ import annotations

from typing import Any

import chess
from fastapi import APIRouter, Request

from ..config import DEFAULT_TC_INCREMENT_SECONDS, DEFAULT_TC_INITIAL_SECONDS
from ..events import Event
from ..play.canonical_hash import FMT_PGN
from ..play.chess_clock import ChessClock, TimeControl
from ..play.human_vs_engine import VIEWING_KEY, HumanVsEngine, ViewModeParams
from ..recent_imports import ROW_FORK_PLY, ROW_GAME_ID, ROW_PARENT_GAME_ID, ROW_SUMMARY
from ._ai_kick import ai_coordinator
from ._http import bad_request

router = APIRouter(prefix="/_test")

_DEFAULT_ENGINE_PATH = "/nonexistent"
_DEFAULT_GAME_ID = "test-game"
_GAME_ID_KEY = ROW_GAME_ID
_ENGINE_NAME_KEY = "engine_name"
_START_FEN_KEY = "start_fen"
_HUMAN_WHITE_KEY = "human_white"
_WHITE_TIME_KEY = "white_time"
_BLACK_TIME_KEY = "black_time"
_BOARD_FEN_KEY = "board_fen"
_TEXT_KEY = "text"
_SUMMARY_KEY = ROW_SUMMARY
_FORK_PLY_KEY = ROW_FORK_PLY
_OK_KEY = "ok"
_HVE_KEY = "hve"


def _board_fen(hve: HumanVsEngine) -> str | None:
    return hve._board.fen() if hve._board is not None else None


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
    engine_path = payload.get("engine_path", _DEFAULT_ENGINE_PATH)
    hve = HumanVsEngine(
        engine_path=engine_path,
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    engine_name = payload.get(_ENGINE_NAME_KEY)
    if engine_name:
        hve._engine_name = engine_name
    view_mode = bool(payload.get("view_mode", False))
    if view_mode:
        await hve.enter_view_mode(ViewModeParams(
            start_fen=payload.get("view_start_fen"),
            moves_uci=payload.get("view_moves_uci", []),
            clock_history=payload.get("view_clock_history"),
        ))
    else:
        start_fen = payload.get(_START_FEN_KEY)
        hve._board = chess.Board(start_fen) if start_fen else chess.Board()
        for uci in payload.get("moves_uci", []):
            hve._board.push_uci(uci)
        hve._eval_history = [None] * len(hve._board.move_stack)
        hve._human_white = bool(payload.get(_HUMAN_WHITE_KEY, True))
        tc_spec = payload.get("tc", {})
        tc = TimeControl(
            initial_seconds=float(tc_spec.get("initial_seconds", DEFAULT_TC_INITIAL_SECONDS)),
            increment_seconds=float(
                tc_spec.get("increment_seconds", DEFAULT_TC_INCREMENT_SECONDS)
            ),
        )
        hve._clock = ChessClock(tc)
        if _WHITE_TIME_KEY in payload:
            hve._clock.white_time = float(payload[_WHITE_TIME_KEY])
        if _BLACK_TIME_KEY in payload:
            hve._clock.black_time = float(payload[_BLACK_TIME_KEY])
        hve._game_id = payload.get(_GAME_ID_KEY, _DEFAULT_GAME_ID)
        hve._clock.start_turn()
    app.state.hve = hve
    return {
        _GAME_ID_KEY: hve._game_id,
        _BOARD_FEN_KEY: _board_fen(hve),
        VIEWING_KEY: view_mode,
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
        raise bad_request("kind required")
    await request.app.state.event_bus.publish(Event(
        kind=kind,
        game_id=payload.get(_GAME_ID_KEY),
        payload=payload.get("payload") or {},
    ))
    return {_OK_KEY: True}


@router.post("/ai/seed_replay")
async def seed_ai_replay(payload: dict, request: Request) -> dict:
    """Prime the AI coordinator's replay buffer with envelope-shaped events.

    A subsequent page load rehydrates the AI panel from these exactly as a
    client reconnecting after a turn would (GET /game/analysis/replay). Lets
    e2e tests exercise the replay-render path without a real LLM turn.

    Body: ``{"events": [{"kind", "payload", "game_id"}]}``."""
    coord = ai_coordinator(request.app.state)
    if coord is None:
        raise bad_request("no ai coordinator")
    events = payload.get("events") or []
    coord.seed_replay(events)
    return {_OK_KEY: True, "count": len(events)}


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
    parent_id = parent[_GAME_ID_KEY]
    child_id = child[_GAME_ID_KEY]
    fork_ply = child[_FORK_PLY_KEY]
    await recents.save(
        fmt=FMT_PGN, text=parent[_TEXT_KEY], summary=parent[_SUMMARY_KEY],
        game_id=parent_id,
    )
    await recents.save(
        fmt=FMT_PGN, text=child[_TEXT_KEY], summary=child[_SUMMARY_KEY],
        game_id=child_id, parent_game_id=parent_id, fork_ply=fork_ply,
    )
    return {
        ROW_PARENT_GAME_ID: parent_id,
        "child_game_id": child_id,
        _FORK_PLY_KEY: fork_ply,
    }


@router.get("/hve/state")
def hve_state(request: Request) -> dict[str, Any]:
    """Read invariants tests assert on. Returns ``hve is None`` if no HVE."""
    hve = request.app.state.hve
    if hve is None:
        return {_HVE_KEY: None}
    board = hve._board
    return {
        _HVE_KEY: True,
        _GAME_ID_KEY: hve._game_id,
        "view_cursor": hve._view_cursor,
        VIEWING_KEY: hve._viewing,
        _HUMAN_WHITE_KEY: hve._human_white,
        "player_name": hve._player_name,
        "paused": hve._paused,
        _BOARD_FEN_KEY: _board_fen(hve),
        "move_stack_uci": [m.uci() for m in board.move_stack] if board is not None else [],
        "n_plies": len(board.move_stack) if board is not None else 0,
    }
