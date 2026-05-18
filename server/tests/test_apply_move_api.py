"""HTTP surface for /api/chess/apply-move (R8 / P10).

Stateless helper called by the tournament-live-game view to advance the
displayed board on bestmove arrival. The endpoint is kept (live caller in
``web/app/tournament-live-game.js``) and pinned here.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry

STARTPOS_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
ENDPOINT = "/api/chess/apply-move"


@pytest.fixture
def client(tmp_path):
    settings = Settings(token="t", auth_disabled=True)
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c


def test_apply_move_accepts_legal_uci(client):
    r = client.post(ENDPOINT, json={"fen": STARTPOS_FEN, "move": "e2e4"})
    assert r.status_code == 200
    # board_from / push round-trips to a fully-specified FEN.
    assert r.json()["fen"].startswith("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b")


def test_apply_move_returns_204_on_late_bestmove(client):
    """The submitted move is legal-shaped but illegal at the supplied FEN
    (position already advanced past it). Server returns 204 so the client
    can drop the stale bestmove silently."""
    # Black to move at AFTER_E4_FEN; e2e4 is not a black move from here.
    r = client.post(ENDPOINT, json={"fen": AFTER_E4_FEN, "move": "e2e4"})
    assert r.status_code == 204
    assert r.content == b""


def test_apply_move_rejects_invalid_fen(client):
    r = client.post(ENDPOINT, json={"fen": "not a fen", "move": "e2e4"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid FEN"


def test_apply_move_rejects_invalid_uci(client):
    r = client.post(ENDPOINT, json={"fen": STARTPOS_FEN, "move": "totally-bogus"})
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid UCI move"
