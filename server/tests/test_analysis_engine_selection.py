"""Engine-only (non-AI) analysis must run on the configured *analysis*
engine, not the active/play engine, and a spawn failure must surface over the
bus + leave ANALYZING instead of silently no-opping."""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import chess.engine
import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.events import EVT_SYSTEM, EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play import tools_engine
from sturddle_view.play.engine_analysis import NoAnalysisEngine, make_analysis_supervisor
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.play.mode import Mode


def test_get_hve_wires_registry_on_every_call(tmp_path):
    """Regression for the bug: a construction site (saved-game restore) that
    omits engines made analysis silently use the play engine. _get_hve must
    wire the registry on every call, regardless of how the HVE was built."""
    settings = Settings(token="t", auth_disabled=True)
    fake = tmp_path / "engine"
    fake.write_text("")
    settings.engine_path = fake
    app = create_app(settings=settings, engine_registry=EngineRegistry(path=tmp_path / "e.json"))
    with TestClient(app) as client:
        # An HVE built WITHOUT engines (mirrors the restore-path bug).
        app.state.hve = HumanVsEngine(
            engine_path=str(fake), bus=app.state.event_bus, settings=settings,
        )
        assert app.state.hve._engines is None
        client.get("/game/pgn")  # runs _get_hve (no engine spawn) then 409s
        assert app.state.hve._engines is app.state.engines


def test_make_analysis_supervisor_uses_pinned_analysis_engine(tmp_path):
    reg = EngineRegistry(path=tmp_path / "engines.json")
    active = reg.add(name="Active", path="/p/active")
    pinned = reg.add(name="Pinned", path="/p/pinned")
    reg.select(active.id)
    settings = Settings()
    settings.analysis_engine_id = pinned.id
    sup = make_analysis_supervisor(reg, settings, EventBus())
    # The bug was non-AI analysis using the ACTIVE engine; it must use the pin.
    assert sup.engine_path == "/p/pinned"


async def test_run_analysis_spawns_pinned_engine_and_surfaces_failure(tmp_path, monkeypatch):
    reg = EngineRegistry(path=tmp_path / "engines.json")
    active = reg.add(name="Active", path="/p/active")
    pinned = reg.add(name="Pinned", path="/p/pinned")  # missing binary
    reg.select(active.id)
    settings = Settings()
    settings.analysis_engine_id = pinned.id
    h = HumanVsEngine(
        engine_path="/p/active", bus=EventBus(), settings=settings, engines=reg,
    )
    h._board = chess.Board()
    h._game_id = "g1"
    h._pre_analysis_mode = Mode.PAUSED
    h._mode = Mode.ANALYZING
    h._publish_board = AsyncMock()
    h._publish_clock = AsyncMock()
    published: list = []

    async def _cap(ev):
        published.append(ev)
    h._bus.publish = _cap

    captured: dict = {}

    async def _fake_spawn(sup, _settings):
        captured["path"] = sup.engine_path
        raise FileNotFoundError(sup.engine_path)
    monkeypatch.setattr(
        "sturddle_view.play.human_vs_engine.spawn_analysis_engine", _fake_spawn,
    )

    await h._run_analysis("g1", chess.Board())

    # Spawned the PINNED analysis engine, not the active/play engine.
    assert captured["path"] == "/p/pinned"
    # Not stranded in ANALYZING; failure surfaced over the bus, named the path.
    assert h._mode is Mode.PAUSED
    err = next(
        ev for ev in published
        if ev.kind == EVT_SYSTEM and ev.payload.get("error") == "analysis_engine_failed"
    )
    assert captured["path"] in err.payload["detail"]  # detail names the failed engine


async def test_ai_search_no_engine_surfaces_searcherror_not_uncaught():
    """The AI tool's launcher (make_analysis_supervisor) raises at *build*
    time when no engine is configured; it must come out as the tool's
    _SearchError envelope, not escape _run_one_search uncaught."""
    def launcher():
        raise NoAnalysisEngine("no analysis engine configured")

    with pytest.raises(tools_engine._SearchError) as ei:
        await tools_engine._run_one_search(
            launcher, chess.Board(), chess.engine.Limit(depth=1),
            bus=EventBus(), game_id="g", cancel_token=CancelToken(),
        )
    assert ei.value.kind == "engine_spawn_failed"
