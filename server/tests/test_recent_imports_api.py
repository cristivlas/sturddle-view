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
    # A is the in-view game right after import; view B so A is deletable
    # without force (the in-view row requires ?force=1).
    client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_B})
    assert client.delete(f"/game/recent-imports/{h}").status_code == 200
    assert len(client.get("/game/recent-imports").json()["entries"]) == 1
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


# ---- Phase 3: import returns and persists stable game_id ----

def test_import_mints_game_id_first_time(client):
    """First import of unseen content -> response carries game_id; same
    id is persisted on the store row and matches the live HVE."""
    r = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["game_id"]
    entries = client.get("/game/recent-imports").json()["entries"]
    assert entries[0]["game_id"] == body["game_id"]


def test_import_dedupes_returns_stored_game_id(client):
    """Second import of the same bytes -> same game_id returned and
    the store still holds a single row with that id."""
    r1 = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    r2 = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    assert r1.json()["game_id"] == r2.json()["game_id"]
    entries = client.get("/game/recent-imports").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["game_id"] == r1.json()["game_id"]


def test_import_supplied_game_id_used_when_hash_new(client):
    """Client-supplied game_id is honored on first import."""
    supplied = "11111111-2222-3333-4444-555555555555"
    r = client.post(
        "/game/import",
        json={"format": "pgn", "text": SAMPLE_PGN_A, "game_id": supplied},
    )
    assert r.status_code == 200, r.text
    assert r.json()["game_id"] == supplied
    entries = client.get("/game/recent-imports").json()["entries"]
    assert entries[0]["game_id"] == supplied


def test_import_supplied_game_id_matching_stored_ok(client):
    """Re-import with the same id as stored -> 200, stored id reused."""
    r1 = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    gid = r1.json()["game_id"]
    r2 = client.post(
        "/game/import",
        json={"format": "pgn", "text": SAMPLE_PGN_A, "game_id": gid},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["game_id"] == gid


def test_import_supplied_game_id_mismatch_409(client):
    """Re-import with a DIFFERENT id than stored -> 409 with both ids."""
    r1 = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    stored = r1.json()["game_id"]
    other = "99999999-8888-7777-6666-555555555555"
    r2 = client.post(
        "/game/import",
        json={"format": "pgn", "text": SAMPLE_PGN_A, "game_id": other},
    )
    assert r2.status_code == 409, r2.text
    body = r2.json()
    assert body["detail"]["code"] == "game_id_mismatch"
    assert body["detail"]["stored_game_id"] == stored
    assert body["detail"]["supplied_game_id"] == other


def test_edit_commit_changed_position_mints_new_game_id(tmp_path):
    """commit_edit on changed FEN -> new game_id, stored on the row."""
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
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        r1 = c.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
        gid_before = r1.json()["game_id"]
        c.post("/game/edit/start", json={}).raise_for_status()
        edited = "r3kbnr/ppp1pppp/2n5/3p4/3P4/2N5/PPP1PPPP/R3KBNR b Kq - 0 1"
        r2 = c.post("/game/edit/commit", json={"fen": edited})
        assert r2.status_code == 200, r2.text
        gid_after = r2.json()["game_id"]
        assert gid_after != gid_before
        # Stored row carries the new id.
        entries = c.get("/game/recent-imports").json()["entries"]
        match = [e for e in entries if e["game_id"] == gid_after]
        assert len(match) == 1
        assert match[0]["format"] == "fen"


def test_edit_commit_unchanged_position_keeps_game_id(tmp_path):
    """commit_edit on unchanged FEN -> same game_id; no new row."""
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
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        r1 = c.post("/game/import", json={"format": "fen", "text": SAMPLE_FEN})
        gid_before = r1.json()["game_id"]
        c.post("/game/edit/start", json={}).raise_for_status()
        r2 = c.post("/game/edit/commit", json={"fen": SAMPLE_FEN})
        assert r2.status_code == 200, r2.text
        assert r2.json()["game_id"] == gid_before
        # No second row introduced.
        entries = c.get("/game/recent-imports").json()["entries"]
        assert len(entries) == 1


# ---- Phase 4: tournament Replay path (game_id == pair_id) ----

PAIR_ID_A = "aaaaaaaa-1111-2222-3333-444444444444"
PAIR_ID_B = "bbbbbbbb-1111-2222-3333-444444444444"


def test_tournament_completion_alone_does_not_touch_recents(client):
    """The recent-imports store is import-driven only. A freshly-built
    client has no Replays yet -> store is empty (regression guard
    against future code paths that try to auto-capture tournament
    games)."""
    entries = client.get("/game/recent-imports").json()["entries"]
    assert entries == []


def test_tournament_replay_stores_pair_id_as_game_id(client):
    """Replay POSTs game_id=pair_id; the store row carries pair_id."""
    r = client.post(
        "/game/import",
        json={"format": "pgn", "text": SAMPLE_PGN_A, "game_id": PAIR_ID_A},
    )
    assert r.status_code == 200
    assert r.json()["game_id"] == PAIR_ID_A
    entries = client.get("/game/recent-imports").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["game_id"] == PAIR_ID_A


def test_tournament_replay_twice_idempotent(client):
    """Two Replays of the same finished tournament game -> single row."""
    body = {"format": "pgn", "text": SAMPLE_PGN_A, "game_id": PAIR_ID_A}
    r1 = client.post("/game/import", json=body)
    r2 = client.post("/game/import", json=body)
    assert r1.json()["game_id"] == PAIR_ID_A
    assert r2.json()["game_id"] == PAIR_ID_A
    assert len(client.get("/game/recent-imports").json()["entries"]) == 1


def test_tournament_replay_rematch_same_bytes_first_pair_id_wins(client, caplog):
    """Rematch produces identical PGN bytes -> hash dedupes -> first
    pair_id wins; second 409s with both ids surfaced."""
    client.post(
        "/game/import",
        json={"format": "pgn", "text": SAMPLE_PGN_A, "game_id": PAIR_ID_A},
    ).raise_for_status()
    with caplog.at_level("WARNING"):
        r = client.post(
            "/game/import",
            json={"format": "pgn", "text": SAMPLE_PGN_A, "game_id": PAIR_ID_B},
        )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["stored_game_id"] == PAIR_ID_A
    assert detail["supplied_game_id"] == PAIR_ID_B
    # WARN log emitted at the API layer.
    assert any("game_id assertion failed" in rec.message for rec in caplog.records)


# ---- Phase 5: by-id endpoint + game_id in payloads ----

def test_list_includes_game_id(client):
    r = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    gid = r.json()["game_id"]
    entries = client.get("/game/recent-imports").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["game_id"] == gid


def test_get_by_hash_includes_game_id(client):
    r = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    gid = r.json()["game_id"]
    h = r.json()["hash"]
    body = client.get(f"/game/recent-imports/{h}").json()
    assert body["game_id"] == gid


def test_get_by_id_returns_same_as_hash_route(client):
    r = client.post("/game/import", json={"format": "pgn", "text": SAMPLE_PGN_A})
    gid = r.json()["game_id"]
    h = r.json()["hash"]
    by_hash = client.get(f"/game/recent-imports/{h}").json()
    by_id = client.get(f"/game/recent-imports/by-id/{gid}").json()
    # ts can differ if the two GETs land in different ms; compare the
    # content-bearing fields.
    assert by_id["hash"] == by_hash["hash"]
    assert by_id["game_id"] == by_hash["game_id"]
    assert by_id["format"] == by_hash["format"]
    assert by_id["text"] == by_hash["text"]
    assert by_id["summary"] == by_hash["summary"]


def test_get_by_id_404_for_unknown(client):
    r = client.get("/game/recent-imports/by-id/no-such-id")
    assert r.status_code == 404


def test_endpoints_require_auth(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as c:
        # No Authorization header -> 401.
        assert c.get("/game/recent-imports").status_code == 401
        assert c.get("/game/recent-imports/abc").status_code == 401
        assert c.delete("/game/recent-imports/abc").status_code == 401


# ---- x-game navigation endpoint contract ----


def _seed_parent_with_child(tmp_path, app, parent_id="gid-parent",
                            child_id="gid-child", fork_ply=4):
    """Inject a parent + linked child directly into the recents store
    so we can exercise the GET endpoint shape without going through
    play-from-here machinery."""
    import asyncio
    store = app.state.recent_imports

    async def seed():
        await store.save(
            fmt="pgn", text='1. e4 e5 *',
            summary={"white": "P"}, game_id=parent_id,
        )
        await store.save(
            fmt="pgn", text='1. d4 d5 *',
            summary={"white": "C"}, game_id=child_id,
            parent_game_id=parent_id, fork_ply=fork_ply,
        )

    asyncio.run(seed())


def test_by_id_response_includes_parent_and_children(tmp_path):
    app = _make_app(tmp_path)
    _seed_parent_with_child(tmp_path, app)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        # Parent: children list non-empty, no parent_game_id.
        parent = c.get("/game/recent-imports/by-id/gid-parent").json()
        assert parent["parent_game_id"] is None
        assert parent["parent_summary"] is None
        assert parent["fork_ply"] is None
        assert len(parent["children"]) == 1
        assert parent["children"][0]["game_id"] == "gid-child"
        assert parent["children"][0]["fork_ply"] == 4
        # Child: parent_game_id + fork_ply set, children empty,
        # parent_summary carries the parent's display.
        child = c.get("/game/recent-imports/by-id/gid-child").json()
        assert child["parent_game_id"] == "gid-parent"
        assert child["parent_summary"] == {"white": "P"}
        assert child["fork_ply"] == 4
        assert child["children"] == []


def test_delete_blocked_by_refs_returns_409(tmp_path):
    app = _make_app(tmp_path)
    _seed_parent_with_child(tmp_path, app)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        # Find parent's hash via the by-id endpoint.
        parent = c.get("/game/recent-imports/by-id/gid-parent").json()
        h_parent = parent["hash"]
        r = c.delete(f"/game/recent-imports/{h_parent}")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["detail"]["error"] == "has_children"
        assert len(body["detail"]["children"]) == 1
        # Parent row still present.
        assert c.get(f"/game/recent-imports/{h_parent}").status_code == 200


def test_delete_child_unblocks_parent(tmp_path):
    app = _make_app(tmp_path)
    _seed_parent_with_child(tmp_path, app)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        parent = c.get("/game/recent-imports/by-id/gid-parent").json()
        child = c.get("/game/recent-imports/by-id/gid-child").json()
        h_parent = parent["hash"]
        h_child = child["hash"]
        # Remove child first -> 200.
        assert c.delete(f"/game/recent-imports/{h_child}").status_code == 200
        # Now parent is unpinned -> 200.
        assert c.delete(f"/game/recent-imports/{h_parent}").status_code == 200
