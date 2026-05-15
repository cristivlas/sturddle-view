from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.api import game as game_api
from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.recent_imports import RecentImports


# A minimal PGN that the parser accepts without complaint.
SAMPLE_PGN_A = '[Event "?"]\n[White "A"]\n[Black "B"]\n[Result "*"]\n\n1. e4 e5 *'
SAMPLE_PGN_B = '[Event "?"]\n[White "C"]\n[Black "D"]\n[Result "*"]\n\n1. d4 d5 *'
SAMPLE_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _make_app(tmp_path):
    """Build a test app with an in-tmp recent-imports store and a stub
    HVE so /game/import (which requires _get_hve) doesn't fail with
    no_engine_configured. View-mode imports never actually launch the
    engine, so a stub path is fine."""
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    e = registry.add(name="MyEngine", path=str(tmp_path / "fake-engine"))
    registry.select(e.id)
    app = create_app(settings=settings, engine_registry=registry)
    app.state.recent_imports = RecentImports.load(root=tmp_path / "imports", cap=5)
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


def test_import_records_recent(client):
    r = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["viewing"] is True
    assert body["hash"]
    assert body["summary"]


def test_import_dedupes_by_hash(client):
    r1 = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    r2 = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    assert r1.json()["hash"] == r2.json()["hash"]
    # And only one entry shows up in the list.
    r = client.get("/game/recent-imports")
    entries = r.json()["entries"]
    assert len(entries) == 1


def test_list_returns_entries_newest_first(client):
    client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_B})
    r = client.get("/game/recent-imports")
    entries = r.json()["entries"]
    assert len(entries) == 2
    # B was imported last -> first in the list.
    assert entries[0]["summary"]  # has a summary
    assert entries[0]["ts"] >= entries[1]["ts"]


def test_get_by_hash_returns_text_and_bumps_ts(client):
    r = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    h_a = r.json()["hash"]
    r = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_B})
    h_b = r.json()["hash"]
    # Currently B is on top.
    listed = client.get("/game/recent-imports").json()["entries"]
    assert listed[0]["hash"] == h_b

    # GET A: returns the text and bumps A's ts.
    got = client.get(f"/game/recent-imports/{h_a}")
    assert got.status_code == 200
    body = got.json()
    assert body["text"] == SAMPLE_PGN_A
    assert body["format"] == "pgn"
    assert body["hash"] == h_a

    # After GET, A should be on top.
    listed = client.get("/game/recent-imports").json()["entries"]
    assert listed[0]["hash"] == h_a


def test_get_by_hash_404_for_unknown(client):
    r = client.get("/game/recent-imports/deadbeef")
    assert r.status_code == 404


def test_delete_drops_entry(client):
    r = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    h = r.json()["hash"]
    assert client.delete(f"/game/recent-imports/{h}").status_code == 200
    assert client.get("/game/recent-imports").json()["entries"] == []
    # Deleting again -> 404.
    assert client.delete(f"/game/recent-imports/{h}").status_code == 404


def test_fen_import_records_recent(client):
    r = client.post("/game/import", json={"format": "fen", "text": SAMPLE_FEN})
    assert r.status_code == 200
    body = r.json()
    assert body["hash"]
    # And it appears in the list with format=fen.
    entries = client.get("/game/recent-imports").json()["entries"]
    assert any(e["format"] == "fen" and e["hash"] == body["hash"] for e in entries)


def test_auto_format_records_detected_format(client):
    r = client.post("/game/import", json={"format": "auto", "text": SAMPLE_FEN})
    assert r.status_code == 200
    entries = client.get("/game/recent-imports").json()["entries"]
    assert entries[0]["format"] == "fen"


def test_failed_import_does_not_record(client):
    r = client.post("/game/import", json={"format": "pgn", "text": "this is not a pgn"})
    assert r.status_code == 400
    # No recents recorded.
    assert client.get("/game/recent-imports").json()["entries"] == []


def test_import_rejects_oversized_text(client, monkeypatch):
    monkeypatch.setattr(game_api, "MAX_IMPORT_TEXT_BYTES", 32)
    big = "1. e4 e5 " * 100
    r = client.post("/game/import", json={"format": "pgn", "text": big})
    assert r.status_code == 400
    assert "too large" in r.json()["detail"]
    # And nothing was recorded.
    assert client.get("/game/recent-imports").json()["entries"] == []


def test_endpoints_require_auth(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as c:
        # No Authorization header -> 401.
        assert c.get("/game/recent-imports").status_code == 401
        assert c.get("/game/recent-imports/abc").status_code == 401
        assert c.delete("/game/recent-imports/abc").status_code == 401
