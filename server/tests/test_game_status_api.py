"""HTTP surface for GET /game/status -- the server-authoritative snapshot
backing the client's discard/replace confirmations (tournament replay).
Client-side mirrors of this state die on page reload; the endpoint must not.
"""
from __future__ import annotations

from sturddle_view.play.mode import Mode

from .conftest import install_active_game

IDLE_STATUS = {
    "in_progress": False,
    "viewing": False,
    "view_hash": None,
    "view_summary": None,
    "analyzing": False,
}
VIEW_HASH = "hash-of-viewed-game"
VIEW_SUMMARY = {"white": "Human", "black": "MyEngine", "result": None}
MOVES = ["e2e4", "e7e5"]


def _status(c):
    r = c.get("/game/status")
    assert r.status_code == 200, r.text
    return r.json()


def test_status_idle_before_any_game(game_api_client):
    c, app, _ = game_api_client
    assert app.state.hve is None
    assert _status(c) == IDLE_STATUS


def test_status_fresh_game_without_moves_is_not_in_progress(game_api_client):
    c, app, engine_path = game_api_client
    install_active_game(app, engine_path=engine_path)
    assert _status(c) == IDLE_STATUS


def test_status_live_game_with_moves_is_in_progress(game_api_client):
    c, app, engine_path = game_api_client
    hve = install_active_game(app, engine_path=engine_path, moves_uci=MOVES)
    # Stale view leftovers must not leak into a play-mode status.
    hve._view_hash = VIEW_HASH
    assert _status(c) == {**IDLE_STATUS, "in_progress": True}


def test_status_paused_game_stays_in_progress(game_api_client):
    c, app, engine_path = game_api_client
    install_active_game(app, engine_path=engine_path, moves_uci=MOVES)
    assert c.post("/game/pause", json={}).status_code == 200
    assert _status(c) == {**IDLE_STATUS, "in_progress": True}


def test_status_viewing_surfaces_hash_and_summary(game_api_client):
    c, app, engine_path = game_api_client
    hve = install_active_game(app, engine_path=engine_path, moves_uci=MOVES)
    hve._mode = Mode.VIEWING
    hve._view_hash = VIEW_HASH
    hve._view_summary = dict(VIEW_SUMMARY)
    assert _status(c) == {
        "in_progress": False,
        "viewing": True,
        "view_hash": VIEW_HASH,
        "view_summary": VIEW_SUMMARY,
        "analyzing": False,
    }


def test_status_analyzing_from_play_is_in_progress(game_api_client):
    c, app, engine_path = game_api_client
    hve = install_active_game(app, engine_path=engine_path, moves_uci=MOVES)
    hve._mode = Mode.ANALYZING
    hve._pre_analysis_mode = Mode.PLAY
    assert _status(c) == {**IDLE_STATUS, "in_progress": True, "analyzing": True}


def test_status_analyzing_from_view_is_viewing(game_api_client):
    c, app, engine_path = game_api_client
    hve = install_active_game(app, engine_path=engine_path, moves_uci=MOVES)
    hve._mode = Mode.ANALYZING
    hve._pre_analysis_mode = Mode.VIEWING
    hve._view_hash = VIEW_HASH
    s = _status(c)
    assert s["viewing"] is True
    assert s["in_progress"] is False
    assert s["view_hash"] == VIEW_HASH
    assert s["analyzing"] is True


def test_status_after_game_end_is_idle(game_api_client):
    c, app, engine_path = game_api_client
    hve = install_active_game(app, engine_path=engine_path, moves_uci=MOVES)
    # Mirror _finalize_game_locked: every game-end path nulls board + id.
    hve._board = None
    hve._game_id = None
    assert _status(c) == IDLE_STATUS
