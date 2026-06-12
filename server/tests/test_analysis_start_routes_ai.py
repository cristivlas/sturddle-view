"""POST /game/analysis/start picks the AI path when settings.ai_enabled.

Closes the loop on the API consolidation: client posts to ONE endpoint;
server picks engine vs AI. With ai_enabled=False the AI coordinator
stays idle (engine go-infinite runs as before, asserted by sister
tests). With ai_enabled=True the coordinator's run() fires and the
engine task does not (see test_start_analysis_ai_gate).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.play.chess_clock import ChessClock, TimeControl
from sturddle_view.play.human_vs_engine import HumanVsEngine, ViewModeParams


def _install_hve(app, *, engine_path) -> HumanVsEngine:
    hve = HumanVsEngine(
        engine_path=engine_path,
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    hve._engine_to_move = AsyncMock()
    hve._run_analysis = AsyncMock()  # don't actually spawn an engine
    hve._board = chess.Board()
    hve._human_white = True
    hve._clock = ChessClock(TimeControl(60.0, 0.0))
    hve._clock.white_time = 60.0
    hve._clock.black_time = 60.0
    hve._game_id = "test-game"
    app.state.hve = hve
    return hve


def _build_client(tmp_path, *, ai_enabled: bool):
    fake_engine = tmp_path / "engine"
    fake_engine.write_text("")
    settings = Settings(token="t", auth_disabled=True)
    settings.engine_path = fake_engine
    settings.ai_enabled = ai_enabled
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    return app, TestClient(app)


@pytest.mark.asyncio
async def test_analysis_start_does_not_kick_ai_when_disabled(tmp_path):
    app, client = _build_client(tmp_path, ai_enabled=False)
    with client:
        hve = _install_hve(app, engine_path=str(tmp_path / "engine"))
        await hve.pause()

        # Spy on the coordinator's run().
        coord_run = AsyncMock()
        app.state.ai_coordinator.run = coord_run

        r = client.post("/game/analysis/start")
        assert r.status_code == 200, r.text

        coord_run.assert_not_called()


@pytest.mark.asyncio
async def test_analysis_start_kicks_ai_when_enabled(tmp_path):
    app, client = _build_client(tmp_path, ai_enabled=True)
    with client:
        hve = _install_hve(app, engine_path=str(tmp_path / "engine"))
        await hve.pause()

        coord_run = AsyncMock()
        app.state.ai_coordinator.run = coord_run

        r = client.post("/game/analysis/start")
        assert r.status_code == 200, r.text

        # The fire-and-forget task may not have awaited run() yet at the
        # moment the response returns. Drain the pinned task to ensure
        # the coordinator was actually invoked.
        ai_task = app.state.ai_task
        assert ai_task is not None
        await ai_task
        coord_run.assert_called_once()


@pytest.mark.asyncio
async def test_analysis_start_from_play_passes_coach_mode(tmp_path):
    app, client = _build_client(tmp_path, ai_enabled=True)
    with client:
        hve = _install_hve(app, engine_path=str(tmp_path / "engine"))
        await hve.pause()

        coord_run = AsyncMock()
        app.state.ai_coordinator.run = coord_run

        r = client.post("/game/analysis/start")
        assert r.status_code == 200, r.text

        await app.state.ai_task
        coord_run.assert_called_once()
        assert coord_run.call_args.kwargs["mode"] == "coach"


@pytest.mark.asyncio
async def test_analysis_start_from_view_passes_commentator_mode(tmp_path):
    app, client = _build_client(tmp_path, ai_enabled=True)
    with client:
        hve = _install_hve(app, engine_path=str(tmp_path / "engine"))
        # Drop into view mode with no moves -- equivalent to opening
        # a PGN replay. start_analysis from VIEWING -> ANALYZING flips
        # _pre_analysis_mode to VIEWING, which is what picks commentator.
        await hve.enter_view_mode(ViewModeParams(
            start_fen=None, moves_uci=[], clock_history=None,
        ))

        coord_run = AsyncMock()
        app.state.ai_coordinator.run = coord_run

        r = client.post("/game/analysis/start")
        assert r.status_code == 200, r.text

        await app.state.ai_task
        coord_run.assert_called_once()
        assert coord_run.call_args.kwargs["mode"] == "commentator"


@pytest.mark.asyncio
async def test_analysis_stop_cancels_ai_turn(tmp_path):
    app, client = _build_client(tmp_path, ai_enabled=True)
    with client:
        hve = _install_hve(app, engine_path=str(tmp_path / "engine"))
        await hve.pause()
        await hve.start_analysis()  # flip to ANALYZING so stop is valid

        cancel_spy = AsyncMock()
        app.state.ai_coordinator.cancel = cancel_spy

        r = client.post("/game/analysis/stop")
        assert r.status_code == 200, r.text

        cancel_spy.assert_called_once()


# The narrator (coach/commentator) registry the model actually calls
# against. top_moves is here so the prompt's "compare your candidates in
# one top_moves call" guidance points at a tool the narrator can dispatch
# -- without it the call returns unknown_tool.
_EXPECTED_NARRATOR_TOOLS = {
    "recommend_move", "top_moves", "report_line", "delegate",
    "piece_at", "validate_move",
}


def test_narrator_registry_exposes_top_moves(tmp_path):
    app, _client = _build_client(tmp_path, ai_enabled=True)
    names = set(app.state.ai_tool_registry.names())
    assert "top_moves" in names


def test_narrator_registry_has_expected_toolset(tmp_path):
    # Pins the narrator toolset: a tool named in the prompt but missing
    # here would dispatch to unknown_tool at runtime.
    app, _client = _build_client(tmp_path, ai_enabled=True)
    assert set(app.state.ai_tool_registry.names()) == _EXPECTED_NARRATOR_TOOLS


def test_top_moves_in_both_narrator_and_verifier_registries(tmp_path):
    # top_moves is shared: the verifier searches with it, and the narrator
    # ranks its own candidates with it. Both registries share one
    # search_cache, so a repeated search isn't paid twice.
    app, _client = _build_client(tmp_path, ai_enabled=True)
    assert "top_moves" in set(app.state.ai_tool_registry.names())
    assert "top_moves" in set(app.state.ai_verifier_registry.names())
