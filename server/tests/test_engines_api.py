from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry


@pytest.fixture
def client(tmp_path):
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        yield c


def test_list_empty(client):
    r = client.get("/engines")
    assert r.status_code == 200
    assert r.json() == {"engines": [], "selected_id": None}


def test_add_then_list(client):
    r = client.post("/engines", json={"name": "Stockfish", "path": "/usr/bin/stockfish"})
    assert r.status_code == 201
    eid = r.json()["id"]

    r = client.get("/engines")
    body = r.json()
    assert len(body["engines"]) == 1
    assert body["engines"][0]["id"] == eid


def test_add_duplicate_409(client):
    client.post("/engines", json={"name": "X", "path": "/p/x"})
    r = client.post("/engines", json={"name": "X", "path": "/p/x"})
    assert r.status_code == 409


def test_update(client):
    eid = client.post("/engines", json={"name": "A", "path": "/p/a"}).json()["id"]
    r = client.patch(f"/engines/{eid}", json={"name": "A2", "options": {"Threads": 4}})
    assert r.status_code == 200
    assert r.json()["name"] == "A2"
    assert r.json()["options"] == {"Threads": 4}


def test_update_unknown_404(client):
    r = client.patch("/engines/nope", json={"name": "x"})
    assert r.status_code == 404


def test_remove(client):
    eid = client.post("/engines", json={"name": "A", "path": "/p/a"}).json()["id"]
    r = client.delete(f"/engines/{eid}")
    assert r.status_code == 204
    assert client.get("/engines").json()["engines"] == []


def test_select(client):
    eid = client.post("/engines", json={"name": "A", "path": "/p/a"}).json()["id"]
    r = client.post(f"/engines/{eid}/select")
    assert r.status_code == 200
    assert r.json() == {"selected_id": eid}
    assert client.get("/engines").json()["selected_id"] == eid


def test_select_unknown_404(client):
    r = client.post("/engines/nope/select")
    assert r.status_code == 404


def test_remove_clears_selection(client):
    eid = client.post("/engines", json={"name": "A", "path": "/p/a"}).json()["id"]
    client.post(f"/engines/{eid}/select")
    client.delete(f"/engines/{eid}")
    assert client.get("/engines").json()["selected_id"] is None


def test_auth_required(tmp_path):
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        # No Authorization header.
        assert c.get("/engines").status_code == 401
        assert c.post("/engines", json={"name": "A", "path": "/p"}).status_code == 401
