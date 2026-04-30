"""HTTP surface for /game/pause and /game/resume."""
from __future__ import annotations

import time
from pathlib import Path

import chess
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl


def _install_active_game(app, *, engine_path: str, human_white: bool = True) -> HumanVsEngine:
    """Plant a fully-initialized HVE on app.state without a real UCI subprocess."""
    hve = HumanVsEngine(
        engine_path=engine_path,
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    hve._engine_to_move = AsyncMock()
    hve._board = chess.Board()
    hve._human_white = human_white
    hve._tc = TimeControl(60.0, 0.0)
    hve._white_time = 60.0
    hve._black_time = 60.0
    hve._game_id = "test-game"
    hve._turn_started_at = time.monotonic()
    app.state.hve = hve
    return hve


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Isolate from the user's persisted settings file (which would otherwise
    # overwrite engine_path inside create_app -> apply_persisted).
    import sturddle_view.config as cfg
    monkeypatch.setattr(cfg, "default_settings_file", lambda: tmp_path / "no_settings.json")

    # _resolve_engine reads settings.engine_path (a Path) when no registry
    # entry is selected. The path does not need to exist — _get_hve only
    # passes it as a string into HumanVsEngine.engine_path comparison.
    fake_engine = tmp_path / "engine"
    fake_engine.write_text("")
    settings = Settings(token="t", auth_disabled=True)
    settings.engine_path = fake_engine
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c, app, str(fake_engine)


def test_pause_then_resume_round_trip(client):
    c, app, engine_path = client
    _install_active_game(app, engine_path=engine_path, human_white=True)

    r = c.post("/game/pause", json={})
    assert r.status_code == 200
    assert app.state.hve.is_paused is True

    r = c.post("/game/resume", json={})
    assert r.status_code == 200
    assert app.state.hve.is_paused is False


def test_pause_off_turn_returns_400(client):
    c, app, engine_path = client
    # Human is black -> on a fresh board, white (engine) is to move.
    _install_active_game(app, engine_path=engine_path, human_white=False)

    r = c.post("/game/pause", json={})
    assert r.status_code == 400
    assert "your turn" in r.json()["detail"]


def test_resume_when_not_paused_is_noop(client):
    c, app, engine_path = client
    _install_active_game(app, engine_path=engine_path, human_white=True)

    r = c.post("/game/resume", json={})
    assert r.status_code == 200, r.text
    assert app.state.hve.is_paused is False
