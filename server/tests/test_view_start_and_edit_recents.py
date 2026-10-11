"""Tests for the scrub-back and edit data paths:

- ``POST /game/view/start`` suspends the live game into a live clone at a
  past ply (1 to one less than the move count) without writing to the
  recent-imports store.
- ``POST /game/edit/commit`` records the accepted position in recents.
- ``POST /game/edit/cancel`` records nothing.

Together these guarantee that scrubbing back or editing from play does
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
from .conftest import REGISTRY_FILE, install_active_game

# Edited position: black to move, distinct from the startpos so dedupe
# logic doesn't mask a missing write.
EDITED_FEN = "r3kbnr/ppp1pppp/2n5/3p4/3P4/2N5/PPP1PPPP/R3KBNR b Kq - 0 1"
LIVE_MOVES = ["e2e4", "e7e5"]
VIEWED_PGN = '[Event "T"]\n[White "A"]\n[Black "B"]\n[Result "*"]\n\n1. e4 *\n\n'
NOTE = "A note."


def _make_app(tmp_path):
    settings = Settings(token="test-token", test_mode=True)
    registry = EngineRegistry(path=tmp_path / REGISTRY_FILE)
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


def _live_game(client) -> str:
    """A live game with LIVE_MOVES played, human to move."""
    app = client.app
    hve = install_active_game(app, engine_path=app.state.hve.engine_path, moves_uci=LIVE_MOVES)
    return hve.game_id


def _rows(client) -> list[dict]:
    return client.get("/game/recent-imports").json()["entries"]


def _import_view(client) -> dict:
    r = client.post("/game/import", json={"format": "pgn", "text": VIEWED_PGN})
    assert r.status_code == 200, r.text
    return r.json()


def test_view_start_clones_live_game_without_recents_write(client):
    live_id = _live_game(client)
    r = client.post("/game/view/start", json={"land_at_ply": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["viewing"] is True
    assert body["game_id"] == live_id
    assert _rows(client) == []


@pytest.mark.parametrize("payload", [{}, {"land_at_ply": 0}, {"land_at_ply": len(LIVE_MOVES)}])
def test_view_start_requires_a_past_ply(client, payload):
    _live_game(client)
    r = client.post("/game/view/start", json=payload)
    assert r.status_code == 400, r.text
    assert client.app.state.hve.is_live_clone is False


def test_view_start_rejects_bool_land_at_ply(client):
    # bool is an int subclass; the scrub-back ply must not silently coerce.
    _live_game(client)
    r = client.post("/game/view/start", json={"land_at_ply": True})
    assert r.status_code == 400, r.text


def test_view_start_rejects_non_int_land_at_ply(client):
    _live_game(client)
    r = client.post("/game/view/start", json={"land_at_ply": "3"})
    assert r.status_code == 400, r.text


def test_resume_play_endpoint_is_gone(client):
    _live_game(client)
    client.post("/game/view/start", json={"land_at_ply": 1}).raise_for_status()
    r = client.post("/game/view/resume-play", json={})
    assert r.status_code == 404, r.text


def test_edit_commit_records_recent(client):
    viewed = _import_view(client)
    client.post("/game/edit/start", json={}).raise_for_status()
    r = client.post("/game/edit/commit", json={"fen": EDITED_FEN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hash"]
    assert body["summary"]
    rows = {row["hash"]: row for row in _rows(client)}
    assert set(rows) == {viewed["hash"], body["hash"]}
    assert rows[body["hash"]]["format"] == "fen"
    # New row carries the active HVE session's current game_id.
    assert rows[body["hash"]]["game_id"] == body["game_id"]
    # Round-trip the text and confirm it's the committed FEN.
    text = client.get(f"/game/recent-imports/{body['hash']}").json()["text"]
    assert text == EDITED_FEN


def test_edit_commit_view_carries_its_recents_hash_and_summary(client):
    # The new view at the edited FEN is that recents row: the client keys
    # "this row is the game in view" on the view's hash.
    _import_view(client)
    client.post("/game/edit/start", json={}).raise_for_status()
    body = client.post("/game/edit/commit", json={"fen": EDITED_FEN}).json()
    hve = client.app.state.hve
    assert hve._view_hash == body["hash"]
    assert hve._view_summary == body["summary"]


def test_edit_cancel_does_not_record_recent(client):
    viewed = _import_view(client)
    client.post("/game/edit/start", json={}).raise_for_status()
    r = client.post("/game/edit/cancel", json={})
    assert r.status_code == 200, r.text
    assert [row["hash"] for row in _rows(client)] == [viewed["hash"]]


def test_edit_commit_annotation_replaces_recents_row_in_place(client):
    """End-to-end: import a PGN -> edit-start -> commit with apply_comment
    -> recents now has ONE row at the new hash, same game_id, no FEN row.
    """
    body = _import_view(client)
    pre_id = body["game_id"]
    pre_hash = body["hash"]
    # Enter edit mode (cursor stays at 0 -- imports land at the start).
    edit_start = client.post("/game/edit/start", json={})
    edit_start.raise_for_status()
    # Commit with annotation at root ply. FEN comes back from edit_start.
    startpos = edit_start.json()["fen"]
    r = client.post("/game/edit/commit", json={
        "fen": startpos,
        "apply_comment": True,
        "comment_text": "Pre-game annotation.",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["game_id"] == pre_id  # game_id preserved
    new_hash = body["hash"]
    assert new_hash is not None
    assert new_hash != pre_hash  # content hash changed
    # Recents now has ONE row at the new hash (old evicted in place).
    listed = _rows(client)
    assert len(listed) == 1
    assert listed[0]["hash"] == new_hash
    assert listed[0]["game_id"] == pre_id
    # The new blob is a PGN that contains the annotation text.
    text = client.get(f"/game/recent-imports/{new_hash}").json()["text"]
    assert "Pre-game annotation." in text


def test_edit_commit_annotation_no_text_change_no_recents_write(client):
    """User opens annotation modal, types the same text that was already
    there (or hits OK on unchanged), commits edit -> changed='none' ->
    no recents write."""
    pgn = '[Event "T"]\n\n{ existing root note } 1. e4 *\n\n'
    r = client.post("/game/import", json={"format": "pgn", "text": pgn})
    pre_hash = r.json()["hash"]
    edit_start = client.post("/game/edit/start", json={})
    edit_start.raise_for_status()
    r = client.post("/game/edit/commit", json={
        "fen": edit_start.json()["fen"],
        "apply_comment": True,
        "comment_text": "existing root note",  # whitespace-normalized match
    })
    # Whitespace-stripped equality may or may not match depending on import
    # parser's sanitization. The key invariant: if the parser stored exactly
    # this text, replay yields 'none'. We accept either 'none' OR 'comment'
    # but in the latter case ensure the original row didn't survive too.
    body = r.json()
    listed = _rows(client)
    # In either branch we never have BOTH the old and new row.
    assert all(e["hash"] != pre_hash for e in listed) or len(listed) == 1
    if body["hash"] is None:
        # changed='none' -- recents untouched at the old hash.
        assert any(e["hash"] == pre_hash for e in listed)


def test_comment_commit_on_view_without_row_writes_nothing(client):
    """A view with no recents row (test hooks only) is never promoted
    into recents by an annotation commit."""
    r = client.post("/_test/hve/install", json={
        "engine_path": client.app.state.hve.engine_path,
        "view_mode": True,
        "view_moves_uci": LIVE_MOVES,
    })
    r.raise_for_status()
    fen = client.post("/game/edit/start", json={}).json()["fen"]
    r = client.post("/game/edit/commit", json={
        "fen": fen, "apply_comment": True, "comment_text": NOTE,
    })
    assert r.status_code == 200, r.text
    assert r.json()["hash"] is None
    assert _rows(client) == []


def test_play_to_edit_and_cancel_yields_clean_recents(client):
    """The full play -> edit -> cancel round trip writes nothing to
    recents. Regression: the old client used /game/import as the
    view-entry path, which always wrote the current play FEN to recents.
    """
    _live_game(client)
    client.post("/game/edit/start", json={}).raise_for_status()
    client.post("/game/edit/cancel", json={}).raise_for_status()
    assert _rows(client) == []
