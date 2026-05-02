from __future__ import annotations

import os
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry


def _make_exec(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    if not sys.platform.startswith("win"):
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _make_fake_uci(path, id_name):
    """Minimal UCI responder: announces `id name <id_name>` then quits cleanly."""
    script = (
        "#!/bin/sh\n"
        "while IFS= read -r line; do\n"
        "  case \"$line\" in\n"
        f"    uci) printf 'id name {id_name}\\nuciok\\n' ;;\n"
        "    isready) printf 'readyok\\n' ;;\n"
        "    quit) exit 0 ;;\n"
        "  esac\n"
        "done\n"
    )
    path.write_text(script)
    if not sys.platform.startswith("win"):
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


@pytest.fixture
def client(tmp_path):
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        yield c


@pytest.fixture
def exe_a(tmp_path):
    return _make_exec(tmp_path / "a")


@pytest.fixture
def exe_b(tmp_path):
    return _make_exec(tmp_path / "b")


def test_list_empty(client):
    r = client.get("/engines")
    assert r.status_code == 200
    assert r.json() == {"engines": [], "selected_id": None}


def test_add_then_list(client, exe_a):
    r = client.post("/engines", json={"name": "Stockfish", "path": exe_a})
    assert r.status_code == 201
    eid = r.json()["id"]

    r = client.get("/engines")
    body = r.json()
    assert len(body["engines"]) == 1
    assert body["engines"][0]["id"] == eid


def test_add_duplicate_409(client, exe_a):
    client.post("/engines", json={"name": "X", "path": exe_a})
    r = client.post("/engines", json={"name": "X", "path": exe_a})
    assert r.status_code == 409


def test_add_rejects_missing_path(client, tmp_path):
    r = client.post("/engines", json={"name": "X", "path": str(tmp_path / "nope")})
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]


def test_add_rejects_directory(client, tmp_path):
    r = client.post("/engines", json={"name": "X", "path": str(tmp_path)})
    assert r.status_code == 400


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX exec bit only")
def test_add_rejects_non_executable(client, tmp_path):
    p = tmp_path / "not-exec"
    p.write_text("hello")
    r = client.post("/engines", json={"name": "X", "path": str(p)})
    assert r.status_code == 400
    assert "not executable" in r.json()["detail"]


def test_update(client, exe_a):
    eid = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    r = client.patch(f"/engines/{eid}", json={"name": "A2", "options": {"Threads": 4}})
    assert r.status_code == 200
    assert r.json()["name"] == "A2"
    assert r.json()["options"] == {"Threads": 4}


def test_update_unknown_404(client):
    r = client.patch("/engines/nope", json={"name": "x"})
    assert r.status_code == 404


def test_remove(client, exe_a):
    eid = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    r = client.delete(f"/engines/{eid}")
    assert r.status_code == 204
    assert client.get("/engines").json()["engines"] == []


def test_select(client, exe_a):
    eid = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    r = client.post(f"/engines/{eid}/select")
    assert r.status_code == 200
    assert r.json() == {"selected_id": eid}
    assert client.get("/engines").json()["selected_id"] == eid


def test_select_unknown_404(client):
    r = client.post("/engines/nope/select")
    assert r.status_code == 404


def test_remove_clears_selection(client, exe_a):
    eid = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    client.post(f"/engines/{eid}/select")
    client.delete(f"/engines/{eid}")
    assert client.get("/engines").json()["selected_id"] is None


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell engine stub")
def test_add_defaults_name_to_uci_id(client, tmp_path):
    """When no name is given, the server uses the engine's UCI `id name`."""
    exe = _make_fake_uci(tmp_path / "weird-binary-name", "FakeEngine 1.2")
    r = client.post("/engines", json={"path": exe})
    assert r.status_code == 201
    assert r.json()["name"] == "FakeEngine 1.2"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell engine stub")
def test_add_falls_back_to_basename_when_probe_fails(client, exe_a):
    """A non-UCI binary still registers; name falls back to the basename."""
    r = client.post("/engines", json={"path": exe_a})
    assert r.status_code == 201
    # exe_a fixture creates the file at tmp_path / "a"
    assert r.json()["name"] == "a"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell engine stub")
def test_explicit_name_overrides_uci_id(client, tmp_path):
    """An explicit name in the payload wins over the engine's UCI announcement."""
    exe = _make_fake_uci(tmp_path / "engine", "FakeEngine 1.2")
    r = client.post("/engines", json={"name": "My Custom Name", "path": exe})
    assert r.status_code == 201
    assert r.json()["name"] == "My Custom Name"


def test_first_add_auto_selects(client, exe_a, exe_b):
    """The first engine registered becomes the active one automatically."""
    eid_a = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    assert client.get("/engines").json()["selected_id"] == eid_a
    # Subsequent adds must NOT steal the selection.
    client.post("/engines", json={"name": "B", "path": exe_b})
    assert client.get("/engines").json()["selected_id"] == eid_a


async def test_probe_engine_returns_error_when_spawn_fails(monkeypatch, exe_a):
    """probe_engine surfaces the spawn failure as a string instead of swallowing it."""
    import chess.engine

    from sturddle_view.engines import probe_engine

    async def boom(*_a, **_kw):
        raise NotImplementedError("nope")

    monkeypatch.setattr(chess.engine, "popen_uci", boom)
    uci_name, schema, error = await probe_engine(exe_a)
    assert uci_name is None
    assert schema == {}
    assert error and "NotImplementedError" in error and "nope" in error


def test_add_includes_probe_error_when_probe_fails(client, monkeypatch, exe_a):
    """A failing probe still registers the engine, but tags the response so the UI can warn."""
    async def fake_probe(_path):
        return None, {}, "NotImplementedError: spawn unsupported"

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", fake_probe)
    r = client.post("/engines", json={"path": exe_a})
    assert r.status_code == 201
    body = r.json()
    assert body["probe_error"] == "NotImplementedError: spawn unsupported"
    assert body["option_schema"] == {}
    # Engine is in the registry and was auto-selected as the first add.
    listed = client.get("/engines").json()
    assert [e["id"] for e in listed["engines"]] == [body["id"]]
    assert listed["selected_id"] == body["id"]


def test_add_omits_probe_error_on_success(client, monkeypatch, exe_a):
    """The success response shape stays unchanged — no `probe_error` key when the probe worked."""
    async def fake_probe(_path):
        return "FakeEngine", {"Hash": {"type": "spin", "default": 16}}, None

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", fake_probe)
    r = client.post("/engines", json={"path": exe_a})
    assert r.status_code == 201
    body = r.json()
    assert "probe_error" not in body
    assert body["option_schema"] == {"Hash": {"type": "spin", "default": 16}}


def test_refresh_schema_502_includes_probe_error_in_detail(client, monkeypatch, exe_a):
    """The dialog's Refresh button needs the underlying reason, not just a generic 502."""
    # First add succeeds (real probe, may yield empty schema — that's fine).
    eid = client.post("/engines", json={"path": exe_a}).json()["id"]

    async def fake_probe(_path):
        return None, {}, "NotImplementedError: spawn unsupported"

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", fake_probe)
    r = client.post(f"/engines/{eid}/refresh-schema")
    assert r.status_code == 502
    assert "NotImplementedError: spawn unsupported" in r.json()["detail"]


def test_auth_required(tmp_path):
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        # No Authorization header.
        assert c.get("/engines").status_code == 401
        assert c.post("/engines", json={"name": "A", "path": "/p"}).status_code == 401
