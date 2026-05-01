"""HTTP surface for /settings — focus on sub-second time-control values."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry


@pytest.fixture
def client(tmp_path):
    settings = Settings(token="t", auth_disabled=True)
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c


def test_subsecond_increment_round_trips(client):
    """Bullet conventions like 1+0.1 must survive PUT then GET intact."""
    r = client.put("/settings", json={"tc_increment_seconds": 0.1})
    assert r.status_code == 200
    assert r.json()["tc_increment_seconds"] == 0.1
    assert client.get("/settings").json()["tc_increment_seconds"] == 0.1


def test_subsecond_initial_round_trips(client):
    """Hyperbullet base times like 0.5s (500ms) are accepted."""
    r = client.put("/settings", json={"tc_initial_seconds": 0.5})
    assert r.status_code == 200
    assert r.json()["tc_initial_seconds"] == 0.5


def test_initial_at_floor_accepted(client):
    """The exact 100ms boundary is the documented minimum and must be accepted."""
    r = client.put("/settings", json={"tc_initial_seconds": 0.1})
    assert r.status_code == 200
    assert r.json()["tc_initial_seconds"] == 0.1


def test_initial_below_floor_rejected(client):
    """Values under the 100ms floor are rejected with 400."""
    r = client.put("/settings", json={"tc_initial_seconds": 0.05})
    assert r.status_code == 400


def test_negative_increment_rejected(client):
    r = client.put("/settings", json={"tc_increment_seconds": -0.1})
    assert r.status_code == 400


def test_zero_increment_accepted(client):
    """0 is the default and must remain valid."""
    r = client.put("/settings", json={"tc_increment_seconds": 0})
    assert r.status_code == 200
    assert r.json()["tc_increment_seconds"] == 0


# ---- Engine defaults round-trip (Defaults tab) ----


def test_engine_defaults_round_trip(client):
    """All five fields PUT then GET intact."""
    payload = {
        "engine_default_threads": 4,
        "engine_default_hash_mb": 1024,
        "engine_default_syzygy_path": "/srv/syzygy",
        "engine_default_book_path": "/srv/book.epd",
        "engine_default_book_plies": 8,
    }
    r = client.put("/settings", json=payload)
    assert r.status_code == 200
    body = r.json()
    for k, v in payload.items():
        assert body[k] == v
    body2 = client.get("/settings").json()
    for k, v in payload.items():
        assert body2[k] == v


def test_engine_defaults_blank_clears(client):
    """Blank string / 0 / null on the wire normalise to None storage."""
    client.put("/settings", json={"engine_default_threads": 4})
    assert client.get("/settings").json()["engine_default_threads"] == 4
    # 0 ⇒ cleared
    r = client.put("/settings", json={"engine_default_threads": 0})
    assert r.status_code == 200
    assert r.json()["engine_default_threads"] is None
    # Same for the path fields when blanked.
    client.put("/settings", json={"engine_default_book_path": "/x"})
    r = client.put("/settings", json={"engine_default_book_path": ""})
    assert r.json()["engine_default_book_path"] is None


def test_engine_defaults_negative_threads_rejected(client):
    r = client.put("/settings", json={"engine_default_threads": -1})
    assert r.status_code == 400


def test_engine_defaults_non_numeric_rejected(client):
    r = client.put("/settings", json={"engine_default_hash_mb": "lots"})
    assert r.status_code == 400
