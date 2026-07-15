"""HTTP surface for /game/pause and /game/resume."""
from __future__ import annotations


from .conftest import install_active_game


def test_pause_then_resume_round_trip(game_api_client):
    c, app, engine_path = game_api_client
    install_active_game(app, engine_path=engine_path, human_white=True)

    r = c.post("/game/pause", json={})
    assert r.status_code == 200
    assert app.state.hve.is_paused is True

    r = c.post("/game/resume", json={})
    assert r.status_code == 200
    assert app.state.hve.is_paused is False


def test_pause_off_turn_returns_400(game_api_client):
    c, app, engine_path = game_api_client
    # Human is black -> on a fresh board, white (engine) is to move.
    install_active_game(app, engine_path=engine_path, human_white=False)

    r = c.post("/game/pause", json={})
    assert r.status_code == 400
    assert "your turn" in r.json()["detail"]


def test_resume_when_not_paused_is_noop(game_api_client):
    c, app, engine_path = game_api_client
    install_active_game(app, engine_path=engine_path, human_white=True)

    r = c.post("/game/resume", json={})
    assert r.status_code == 200, r.text
    assert app.state.hve.is_paused is False
