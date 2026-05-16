"""Tests for the play -> view -> edit data path:

- ``POST /game/view/start`` enters view mode at the current board's FEN
  without writing to the recent-imports store.
- ``POST /game/edit/commit`` records the accepted position in recents.
- ``POST /game/edit/cancel`` records nothing.

Together these guarantee that entering the editor from play mode does
not pollute the import history, but a committed edit is recallable.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.recent_imports import RecentImports

SAMPLE_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# Edited position: black to move, distinct from the startpos so dedupe
# logic doesn't mask a missing write.
EDITED_FEN = "r3kbnr/ppp1pppp/2n5/3p4/3P4/2N5/PPP1PPPP/R3KBNR b Kq - 0 1"


def _make_app(tmp_path):
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    e = registry.add(name="MyEngine", path=str(tmp_path / "fake-engine"))
    registry.select(e.id)
    app = create_app(settings=settings, engine_registry=registry)
    app.state.recent_imports = RecentImports.load(root=tmp_path / "imports", cap=10)
    app.state.hve = HumanVsEngine(
        engine_path=str(tmp_path / "fake-engine"),
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    return app


@pytest.fixture
def client(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        yield c


def test_view_start_enters_view_without_recents_write(client):
    r = client.post("/game/view/start", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["viewing"] is True
    assert body["game_id"]
    # No recents entry was created.
    listed = client.get("/game/recent-imports").json()["entries"]
    assert listed == []


def test_edit_commit_records_recent(client):
    client.post("/game/view/start", json={}).raise_for_status()
    client.post("/game/edit/start", json={}).raise_for_status()
    r = client.post("/game/edit/commit", json={"fen": EDITED_FEN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hash"]
    assert body["summary"]
    listed = client.get("/game/recent-imports").json()["entries"]
    assert len(listed) == 1
    assert listed[0]["format"] == "fen"
    # Round-trip the text and confirm it's the committed FEN.
    h = listed[0]["hash"]
    text = client.get(f"/game/recent-imports/{h}").json()["text"]
    assert text == EDITED_FEN


def test_edit_cancel_does_not_record_recent(client):
    client.post("/game/view/start", json={}).raise_for_status()
    client.post("/game/edit/start", json={}).raise_for_status()
    r = client.post("/game/edit/cancel", json={})
    assert r.status_code == 200, r.text
    listed = client.get("/game/recent-imports").json()["entries"]
    assert listed == []


def test_play_to_edit_via_view_start_yields_clean_recents(client):
    """The full play -> view -> edit -> cancel round trip writes nothing
    to recents. Regression: the old client used /game/import as the
    view-entry path, which always wrote the current play FEN to recents.
    """
    client.post("/game/view/start", json={}).raise_for_status()
    client.post("/game/edit/start", json={}).raise_for_status()
    client.post("/game/edit/cancel", json={}).raise_for_status()
    listed = client.get("/game/recent-imports").json()["entries"]
    assert listed == []
