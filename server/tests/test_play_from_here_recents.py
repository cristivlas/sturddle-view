"""Full-stack play-from-here fork flow over HTTP.

import parent -> goto -> play-from-here -> resign -> auto-view re-import
(exactly what the client does on game_result) -> fork again -> assert no
row overwrite and both fork links recorded. The repeat-fork test guards
the identical-content dedup: the second fork's game_id must alias onto
the first row and stay reachable by id (the auto-view GET depends on it).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.engines import EngineRegistry
from sturddle_view.config import Settings

from .conftest import _write_uci_stub

PARENT_PGN = (
    '[Event "?"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n'
    "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0\n"
)

LEGAL_MOVE_BODY = (
    "    elif line.startswith('position'):\n"
    "        parts = line.split()\n"
    "        if 'fen' in parts:\n"
    "            i = parts.index('fen')\n"
    "            j = parts.index('moves') if 'moves' in parts else len(parts)\n"
    "            board = chess.Board(' '.join(parts[i + 1:j]))\n"
    "        else:\n"
    "            board = chess.Board()\n"
    "        if 'moves' in parts:\n"
    "            for u in parts[parts.index('moves') + 1:]:\n"
    "                board.push_uci(u)\n"
    "    elif line.startswith('go'):\n"
    "        m = next(iter(board.legal_moves))\n"
    "        sys.stdout.write('bestmove %s\\n' % m.uci()); sys.stdout.flush()\n"
)


@pytest.fixture
def client(tmp_path):
    engine = _write_uci_stub(
        tmp_path, "legalmover", LEGAL_MOVE_BODY,
        pre_loop="import chess\nboard = chess.Board()\n",
    )
    settings = Settings(token="t", auth_disabled=True)
    settings.engine_path = engine
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c


def _rows(c):
    r = c.get("/game/recent-imports")
    assert r.status_code == 200, r.text
    return r.json()["entries"]


def _by_id(c, gid):
    r = c.get(f"/game/recent-imports/by-id/{gid}")
    assert r.status_code == 200, r.text
    return r.json()


def test_fork_no_overwrite_and_links(client):
    c = client

    # 1. Import parent.
    r = c.post("/game/import", json={"format": "pgn", "text": PARENT_PGN})
    assert r.status_code == 200, r.text
    parent_id = r.json()["game_id"]

    # 2. Navigate to ply 4, fork.
    assert c.post("/game/view/goto", json={"ply": 4}).status_code == 200
    r = c.post("/game/view/play-from-here", json={})
    assert r.status_code == 200, r.text
    child_id = r.json()["game_id"]
    assert child_id != parent_id, "FORK DID NOT MINT A NEW GAME"

    # 3. Resign immediately (child keeps the 4 seeded plies).
    assert c.post("/game/resign", json={}).status_code == 200

    rows = _rows(c)
    assert len(rows) == 2, f"expected parent+child rows, got {rows}"
    parent = _by_id(c, parent_id)
    child = _by_id(c, child_id)
    assert "Qxf7#" in parent["text"], "PARENT TEXT OVERWRITTEN"
    assert child["parent_game_id"] == parent_id, "CHILD LOST PARENT LINK"
    assert child["fork_ply"] == 4
    assert parent["children"], "PARENT HAS NO CHILDREN REF"

    # 4. Auto-view (what play.js does on game_result).
    r = c.post(
        "/game/import",
        json={"format": child["format"], "text": child["text"], "land_at_ply": 4},
    )
    assert r.status_code == 200, r.text
    assert r.json()["game_id"] == child_id, "AUTO-VIEW CHANGED THE GAME ID"

    rows = _rows(c)
    assert len(rows) == 2, f"auto-view added/removed rows: {rows}"
    child = _by_id(c, child_id)
    assert child["parent_game_id"] == parent_id, "AUTO-VIEW DROPPED PARENT LINK"
    assert _by_id(c, parent_id)["children"], "AUTO-VIEW DROPPED CHILDREN REF"

    # 5. Fork again from the auto-viewed child at ply 2.
    assert c.post("/game/view/goto", json={"ply": 2}).status_code == 200
    r = c.post("/game/view/play-from-here", json={})
    assert r.status_code == 200, r.text
    gchild_id = r.json()["game_id"]
    assert gchild_id not in (parent_id, child_id), "SECOND FORK REUSED AN ID"
    assert c.post("/game/resign", json={}).status_code == 200

    rows = _rows(c)
    assert len(rows) == 3, f"expected 3 rows, got {rows}"
    assert "Qxf7#" in _by_id(c, parent_id)["text"], "PARENT OVERWRITTEN AT END"
    gchild = _by_id(c, gchild_id)
    assert gchild["parent_game_id"] == child_id, "GRANDCHILD LOST PARENT LINK"
    assert _by_id(c, child_id)["children"], "CHILD HAS NO CHILDREN REF"


def test_repeat_fork_same_ply(client):
    """Fork at the same ply twice, resigning both -- identical PGN text.
    The second child must still be reachable by id (auto-view GETs it)."""
    c = client
    r = c.post("/game/import", json={"format": "pgn", "text": PARENT_PGN})
    parent_id = r.json()["game_id"]

    assert c.post("/game/view/goto", json={"ply": 4}).status_code == 200
    child1 = c.post("/game/view/play-from-here", json={}).json()["game_id"]
    assert c.post("/game/resign", json={}).status_code == 200
    assert _by_id(c, child1)["parent_game_id"] == parent_id

    # Client auto-views child1, then user forks again from the same ply.
    got = _by_id(c, child1)
    c.post("/game/import", json={"format": got["format"], "text": got["text"]})
    assert c.post("/game/view/goto", json={"ply": 4}).status_code == 200
    child2 = c.post("/game/view/play-from-here", json={}).json()["game_id"]
    assert c.post("/game/resign", json={}).status_code == 200

    r = c.get(f"/game/recent-imports/by-id/{child2}")
    assert r.status_code == 200, (
        f"SECOND FORK UNREACHABLE BY ID (status {r.status_code}); "
        f"rows={_rows(c)}"
    )
