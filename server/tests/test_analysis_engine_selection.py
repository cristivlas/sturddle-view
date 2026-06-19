"""Engine-only (non-AI) analysis must run on the configured *analysis*
engine, not the active/play engine, and a spawn failure must surface over the
bus + leave ANALYZING instead of silently no-opping."""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess

from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.events import EVT_SYSTEM, EventBus
from sturddle_view.play.engine_analysis import make_analysis_supervisor
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.play.mode import Mode


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
    # Not stranded in ANALYZING; failure surfaced over the bus.
    assert h._mode is Mode.PAUSED
    assert any(
        ev.kind == EVT_SYSTEM and ev.payload.get("error") == "analysis_engine_failed"
        for ev in published
    )
