"""Live application of engine settings while a HvE game is active.

Drives HumanVsEngine with a stub engine + monkey-patched _ensure_engine,
mirroring the pattern in test_pause.py / test_takeback.py. No real UCI
subprocess is spawned.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl


class _StubEngine:
    """Minimal stand-in for chess.engine.UciProtocol. Records quit() so
    tests can assert the live process was shot in the head."""

    def __init__(self) -> None:
        self.quit_called = False

    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        self.quit_called = True


@pytest.fixture
def hve():
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


async def test_live_apply_quits_engine(hve):
    """Idle engine -- quit() is called so the next _ensure_engine respawns."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    # Force a live engine to exist (new_game doesn't spawn on human's turn).
    engine = await hve._ensure_engine()
    assert hve._engine is engine

    await hve.apply_engine_settings_live()
    assert engine.quit_called
    assert hve._engine is None


async def test_live_apply_rekicks_on_engine_turn(hve):
    """When it's the engine's move, kick _engine_to_move again after respawn."""
    # Human plays black -> engine (white) starts; new_game will call
    # _engine_to_move once. Reset the mock before our live-apply call so
    # we can assert the second kick distinctly.
    await hve.new_game(human_white=False, tc=TimeControl(60.0, 0.0))
    hve._engine = _StubEngine()  # pretend a search is parked
    hve._engine_to_move.reset_mock()

    await hve.apply_engine_settings_live()
    hve._engine_to_move.assert_awaited_once()


async def test_live_apply_no_kick_on_human_turn(hve):
    """Human-to-move: quit the engine but don't kick a new search."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    hve._engine = _StubEngine()
    hve._engine_to_move.reset_mock()

    await hve.apply_engine_settings_live()
    hve._engine_to_move.assert_not_awaited()


async def test_live_apply_skipped_during_analysis(hve):
    """Analysis runs on a throwaway engine; live-apply must not touch it."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.pause()
    # Fake analysis mode without actually spawning the analysis task --
    # start_analysis would try to spin up a real engine via _spawn_engine.
    hve._analysis_mode = True
    engine = _StubEngine()
    hve._engine = engine
    hve._engine_to_move.reset_mock()

    await hve.apply_engine_settings_live()
    assert not engine.quit_called
    assert hve._engine is engine
    hve._engine_to_move.assert_not_awaited()


async def test_live_apply_no_op_without_game(hve):
    """Fresh HvE, no new_game called -- must be safe to invoke."""
    await hve.apply_engine_settings_live()
    assert hve._engine is None
    hve._engine_to_move.assert_not_awaited()


async def test_live_apply_no_kick_when_paused(hve):
    """Paused game: respawn the engine but don't kick (resume handles it)."""
    # human=white -> starts on human's turn so pause() is legal.
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.pause()
    hve._engine = _StubEngine()
    hve._engine_to_move.reset_mock()

    await hve.apply_engine_settings_live()
    hve._engine_to_move.assert_not_awaited()


# ---------- API-layer tests ----------


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """FastAPI TestClient with a real registry on disk and a stub HvE
    pre-attached so the route handlers' getattr(s, "hve", None) returns it."""
    settings = Settings(token="test-token", auth_disabled=True)
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    hve_stub = MagicMock()
    hve_stub.apply_engine_settings_live = AsyncMock()
    hve_stub.shutdown = AsyncMock()  # invoked by app lifespan on teardown
    hve_stub.set_engine_name = MagicMock()
    hve_stub.set_engine_options = MagicMock()
    hve_stub.set_engine_args = MagicMock()
    hve_stub.set_engine_env = MagicMock()
    app.state.hve = hve_stub
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        yield c, app, hve_stub


def _add_engine(client, tmp_path, name="A"):
    """Register a stub engine binary that exists + is executable."""
    import stat
    import sys

    p = tmp_path / f"engine_{name}"
    p.write_text("#!/bin/sh\nexit 0\n")
    if not sys.platform.startswith("win"):
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    r = client.post("/engines", json={"name": name, "path": str(p)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_patch_selected_engine_triggers_live_apply(api_client, tmp_path):
    client, _app, hve = api_client
    eid = _add_engine(client, tmp_path, "A")
    # add() auto-selects the first engine, so eid is selected.
    r = client.patch(f"/engines/{eid}", json={"options": {"Threads": 4}})
    assert r.status_code == 200
    hve.apply_engine_settings_live.assert_awaited_once()
    hve.set_engine_options.assert_called()


def test_patch_unselected_engine_skips_live_apply(api_client, tmp_path):
    client, _app, hve = api_client
    _eid_a = _add_engine(client, tmp_path, "A")  # auto-selected
    eid_b = _add_engine(client, tmp_path, "B")
    r = client.patch(f"/engines/{eid_b}", json={"options": {"Threads": 8}})
    assert r.status_code == 200
    hve.apply_engine_settings_live.assert_not_awaited()


def test_settings_put_with_engine_key_triggers_live_apply(api_client):
    client, _app, hve = api_client
    r = client.put("/settings", json={"engine_default_threads": 4})
    assert r.status_code == 200
    hve.apply_engine_settings_live.assert_awaited_once()


def test_settings_put_with_hash_triggers_live_apply(api_client):
    client, _app, hve = api_client
    r = client.put("/settings", json={"engine_default_hash_mb": 256})
    assert r.status_code == 200
    hve.apply_engine_settings_live.assert_awaited_once()


def test_settings_put_with_syzygy_triggers_live_apply(api_client, tmp_path):
    client, _app, hve = api_client
    sp = tmp_path / "sz"
    sp.mkdir()
    r = client.put("/settings", json={"engine_default_syzygy_path": str(sp)})
    assert r.status_code == 200
    hve.apply_engine_settings_live.assert_awaited_once()


def test_settings_put_without_engine_keys_skips_live_apply(api_client):
    client, _app, hve = api_client
    r = client.put("/settings", json={"pgn_autosave": True})
    assert r.status_code == 200
    hve.apply_engine_settings_live.assert_not_awaited()
