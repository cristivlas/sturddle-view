from __future__ import annotations

import json
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.tournament.store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_IDLE,
    STATUS_RUNNING,
    STATUS_STOPPED,
    TournamentStore,
)


def _make_exec(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    if not sys.platform.startswith("win"):
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _make_echo_uci(path):
    """UCI responder that round-trips its launch profile.

    On `uci`, it announces:
      - ``id name argv:<argv[1:] joined by '|'>``
      - ``id author env:<value of $SV_TEST_ENV>``
    Then ``uciok``. Lets a probe assert the engine subprocess actually
    saw the args/env we asked for, without depending on a third-party
    UCI binary.
    """
    py = path.with_suffix(".py")
    py.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "argv_joined = '|'.join(sys.argv[1:])\n"
        "env_val = os.environ.get('SV_TEST_ENV', '')\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    line = line.strip()\n"
        "    if line == 'uci':\n"
        "        sys.stdout.write(f'id name argv:{argv_joined}\\n')\n"
        "        sys.stdout.write(f'id author env:{env_val}\\n')\n"
        "        sys.stdout.write('uciok\\n')\n"
        "        sys.stdout.flush()\n"
        "    elif line == 'isready':\n"
        "        sys.stdout.write('readyok\\n'); sys.stdout.flush()\n"
        "    elif line == 'quit':\n"
        "        break\n"
    )
    if sys.platform.startswith("win"):
        cmd = path.with_suffix(".cmd")
        cmd.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        return str(cmd)
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(py)


def _make_fake_uci(path, id_name):
    """Minimal UCI responder: announces `id name <id_name>` (skipped when
    empty) then quits cleanly.

    Implementation is a Python script for portability. On POSIX we rely on
    the ``#!`` shebang + exec bit; on Windows we drop a tiny ``.cmd``
    wrapper next to it and return the wrapper's path so it looks like a
    single-binary engine to ``_validate_engine_path`` and ``popen_uci``.
    """
    announce = f"id name {id_name}\\n" if id_name else ""
    py = path.with_suffix(".py")
    py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        # Use readline() rather than `for line in sys.stdin`: the iterator
        # form has read-ahead buffering that can deadlock a UCI handshake
        # when the engine should reply line-by-line.
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    line = line.strip()\n"
        "    if line == 'uci':\n"
        f"        sys.stdout.write('{announce}uciok\\n')\n"
        "        sys.stdout.flush()\n"
        "    elif line == 'isready':\n"
        "        sys.stdout.write('readyok\\n'); sys.stdout.flush()\n"
        "    elif line == 'quit':\n"
        "        break\n"
    )
    if sys.platform.startswith("win"):
        cmd = path.with_suffix(".cmd")
        cmd.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        return str(cmd)
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(py)


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
    # Probeable UCI binary so /engines POST succeeds; tests that exercise
    # probe-failure behavior monkeypatch ``probe_engine`` to override.
    return _make_fake_uci(tmp_path / "a", "EngineA")


@pytest.fixture
def exe_b(tmp_path):
    return _make_fake_uci(tmp_path / "b", "EngineB")


def test_list_empty(client):
    r = client.get("/engines")
    assert r.status_code == 200
    assert r.json() == {"engines": [], "selected_id": None}


def test_add_then_list(client, exe_a):
    r = client.post("/engines", json={"name": "MyEngine", "path": exe_a})
    assert r.status_code == 201
    eid = r.json()["id"]

    r = client.get("/engines")
    body = r.json()
    assert len(body["engines"]) == 1
    assert body["engines"][0]["id"] == eid


def test_add_duplicate_user_name_409(client, exe_a, exe_b):
    """User-supplied names must collide → 409 (different paths, same name)."""
    client.post("/engines", json={"name": "X", "path": exe_a})
    r = client.post("/engines", json={"name": "X", "path": exe_b})
    assert r.status_code == 409


def test_add_auto_suffixes_derived_name(client, tmp_path):
    """Two installs of the same UCI engine: derived names are auto-suffixed."""
    exe1 = _make_fake_uci(tmp_path / "engine1", "FakeEngine 1.2")
    exe2 = _make_fake_uci(tmp_path / "engine2", "FakeEngine 1.2")
    r1 = client.post("/engines", json={"path": exe1})
    r2 = client.post("/engines", json={"path": exe2})
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["name"] == "FakeEngine 1.2"
    assert r2.json()["name"] == "FakeEngine 1.2 (2)"


def test_patch_rename_collision_409(client, exe_a, exe_b):
    client.post("/engines", json={"name": "A", "path": exe_a})
    bid = client.post("/engines", json={"name": "B", "path": exe_b}).json()["id"]
    r = client.patch(f"/engines/{bid}", json={"name": "A"})
    assert r.status_code == 409


def test_rating_roundtrip_and_patch_semantics(client, exe_a):
    """Omitted = untouched, null = clear, value = set."""
    created = client.post(
        "/engines", json={"name": "E", "path": exe_a, "rating": 3000},
    ).json()
    eid = created["id"]
    assert created["rating"] == 3000

    client.patch(f"/engines/{eid}", json={"name": "E2"})
    assert client.get(f"/engines/{eid}").json()["rating"] == 3000

    client.patch(f"/engines/{eid}", json={"rating": 3150})
    assert client.get(f"/engines/{eid}").json()["rating"] == 3150

    client.patch(f"/engines/{eid}", json={"rating": None})
    assert client.get(f"/engines/{eid}").json()["rating"] is None


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


def test_add_defaults_name_to_uci_id(client, tmp_path):
    """When no name is given, the server uses the engine's UCI `id name`."""
    exe = _make_fake_uci(tmp_path / "weird-binary-name", "FakeEngine 1.2")
    r = client.post("/engines", json={"path": exe})
    assert r.status_code == 201
    assert r.json()["name"] == "FakeEngine 1.2"


def test_explicit_name_overrides_uci_id(client, tmp_path):
    """An explicit name in the payload wins over the engine's UCI announcement."""
    exe = _make_fake_uci(tmp_path / "engine", "FakeEngine 1.2")
    r = client.post("/engines", json={"name": "My Custom Name", "path": exe})
    assert r.status_code == 201
    assert r.json()["name"] == "My Custom Name"


def test_add_persists_uci_name(client, exe_a):
    """The probed UCI `id name` is stored alongside the display name."""
    r = client.post("/engines", json={"name": "A", "path": exe_a})
    assert r.status_code == 201
    assert r.json()["uci_name"] == "EngineA"


def test_add_stores_empty_uci_name_when_unannounced(client, tmp_path):
    """'' (not null) marks "probed, no id name" so clients skip re-probing."""
    exe = _make_fake_uci(tmp_path / "mute", "")
    r = client.post("/engines", json={"name": "A", "path": exe})
    assert r.status_code == 201
    assert r.json()["uci_name"] == ""


def test_refresh_schema_backfills_uci_name(client, monkeypatch, exe_a):
    """Entries that predate uci_name capture get it on refresh-schema."""
    eid = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    # Simulate a legacy entry registered before uci_name persistence.
    client.app.state.engines.get(eid).uci_name = None

    async def fake_probe(_path, args=None, env=None):
        return "EngineA", {"Hash": {"type": "spin", "default": 16}}, None

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", fake_probe)
    r = client.post(f"/engines/{eid}/refresh-schema")
    assert r.status_code == 200
    assert r.json()["uci_name"] == "EngineA"


def test_list_lazy_schema_capture_also_stores_uci_name(client, monkeypatch, exe_a):
    """GET /engines' lazy schema capture persists uci_name in the same update."""
    async def probe_nothing(_path, args=None, env=None):
        return None, {}, None

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", probe_nothing)
    client.post("/engines", json={"name": "A", "path": exe_a})

    async def probe_full(_path, args=None, env=None):
        return "EngineA", {"Hash": {"type": "spin", "default": 16}}, None

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", probe_full)
    listed = client.get("/engines").json()["engines"][0]
    assert listed["uci_name"] == "EngineA"


def test_refresh_schema_ok_for_optionless_engine(client, exe_a):
    """A clean probe with zero options is a 200, not a 502, and persists
    uci_name so the entry stops re-probing."""
    eid = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    client.app.state.engines.get(eid).uci_name = None
    r = client.post(f"/engines/{eid}/refresh-schema")
    assert r.status_code == 200
    assert r.json()["uci_name"] == "EngineA"
    assert r.json()["option_schema"] == {}


def test_list_does_not_reprobe_probed_optionless_engine(client, monkeypatch, exe_a):
    """Once uci_name is captured, an empty schema alone must not re-spawn on GET."""
    client.post("/engines", json={"name": "A", "path": exe_a})

    calls = []

    async def counting_probe(*_a, **_kw):
        calls.append(1)
        return "EngineA", {}, None

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", counting_probe)
    client.get("/engines")
    assert calls == []


def test_list_probe_false_skips_lazy_schema_capture(client, monkeypatch, exe_a):
    """?probe=false must not spawn engines -- read-only consumers (the
    start-time drift check) pay no probe per unprobed registry entry."""
    async def probe_nothing(_path, args=None, env=None):
        return None, {}, None

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", probe_nothing)
    client.post("/engines", json={"name": "A", "path": exe_a})

    calls = []

    async def counting_probe(*_a, **_kw):
        calls.append(1)
        return "EngineA", {}, None

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", counting_probe)
    listed = client.get("/engines?probe=false").json()["engines"]
    assert calls == []
    assert listed[0]["name"] == "A"


def test_patch_auto_suffix_resolves_collision(client, exe_a, exe_b):
    """auto_suffix (Reset-derived name) suffixes instead of 409ing."""
    client.post("/engines", json={"name": "A", "path": exe_a})
    bid = client.post("/engines", json={"name": "B", "path": exe_b}).json()["id"]
    r = client.patch(f"/engines/{bid}", json={"name": "A", "auto_suffix": True})
    assert r.status_code == 200
    assert r.json()["name"] == "A (2)"


def test_patch_persists_option_schema_and_uci_name(client, exe_a):
    """Save after an in-dialog Refresh pushes the fresher schema and uci_name."""
    eid = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    schema = {"Hash": {"type": "spin", "default": 16}}
    r = client.patch(f"/engines/{eid}", json={"option_schema": schema, "uci_name": "EngineA 2.0"})
    assert r.status_code == 200
    listed = client.get("/engines").json()["engines"][0]
    assert listed["option_schema"] == schema
    assert listed["uci_name"] == "EngineA 2.0"


def test_patch_path_change_invalidates_uci_name(client, exe_a, exe_b):
    """Swapping the binary clears the cached UCI identity for re-probe."""
    eid = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    assert client.get(f"/engines/{eid}").json()["uci_name"] == "EngineA"
    r = client.patch(f"/engines/{eid}", json={"path": exe_b})
    assert r.status_code == 200
    assert r.json()["uci_name"] is None


def test_registry_loads_legacy_entry_without_uci_name(tmp_path):
    """Pre-uci_name registry files load with uci_name=None (client backfills)."""
    p = tmp_path / "engines.json"
    p.write_text(json.dumps({
        "engines": [{"id": "abc", "name": "A", "path": "/x"}],
        "selected_id": None,
    }), encoding="utf-8")
    reg = EngineRegistry(path=p)
    assert reg.get("abc").uci_name is None


def test_first_add_auto_selects(client, exe_a, exe_b):
    """The first engine registered becomes the active one automatically."""
    eid_a = client.post("/engines", json={"name": "A", "path": exe_a}).json()["id"]
    assert client.get("/engines").json()["selected_id"] == eid_a
    # Subsequent adds must NOT steal the selection.
    client.post("/engines", json={"name": "B", "path": exe_b})
    assert client.get("/engines").json()["selected_id"] == eid_a


async def test_probe_engine_returns_error_when_spawn_fails(monkeypatch, exe_a):
    """probe_engine surfaces the spawn failure as a structured {code, message} dict."""
    import chess.engine

    from sturddle_view.engines import probe_engine

    async def boom(*_a, **_kw):
        raise NotImplementedError("nope")

    monkeypatch.setattr(chess.engine, "popen_uci", boom)
    uci_name, schema, error = await probe_engine(exe_a)
    assert uci_name is None
    assert schema == {}
    assert isinstance(error, dict)
    assert error["code"] == "engine_probe_failed"
    assert error["message"]
    # Raw exception type names must not leak into the user-facing message.
    assert "NotImplementedError" not in error["message"]


@pytest.mark.parametrize("exc,expected_code", [
    (FileNotFoundError(2, "No such file or directory"), "engine_path_not_found"),
    (PermissionError(13, "Permission denied"), "engine_permission_denied"),
    # OSError covers WinError 193 ("not a valid Win32 application") on Windows
    # and ENOEXEC ("Exec format error") on POSIX -- both reach probe_engine
    # as a plain OSError when the file exists but is not runnable.
    (OSError(8, "Exec format error"), "engine_not_launchable"),
    (RuntimeError("something weird"), "engine_probe_failed"),
])
async def test_probe_engine_classifies_spawn_errors(monkeypatch, exe_a, exc, expected_code):
    """probe_engine classifies spawn-time exceptions by type, cross-platform."""
    import chess.engine

    from sturddle_view.engines import probe_engine

    async def boom(*_a, **_kw):
        raise exc

    monkeypatch.setattr(chess.engine, "popen_uci", boom)
    _name, _schema, error = await probe_engine(exe_a)
    assert isinstance(error, dict)
    assert error["code"] == expected_code
    assert error["message"]
    assert type(exc).__name__ not in error["message"]


async def test_probe_engine_logs_classified_failures_at_warning(monkeypatch, exe_a, caplog):
    """Known failure classes log a one-liner WARNING, not an ERROR + stacktrace.

    A broken legacy engine entry gets probed on every GET /engines; pumping
    a full traceback into the server log on each list call is just noise.
    """
    import logging

    import chess.engine

    from sturddle_view.engines import probe_engine

    async def boom(*_a, **_kw):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(chess.engine, "popen_uci", boom)
    with caplog.at_level(logging.DEBUG, logger="sturddle_view.engines"):
        await probe_engine(exe_a)
    records = [r for r in caplog.records if r.name == "sturddle_view.engines"]
    assert records, "expected at least one log record from probe_engine"
    # No ERROR-level records, no exception info attached.
    for r in records:
        assert r.levelno < logging.ERROR, (
            f"classified failure should not log at ERROR (got {r.levelname}: {r.getMessage()})"
        )
        assert r.exc_info is None, "stacktrace should not be attached for classified failures"


async def test_probe_engine_logs_unclassified_failures_at_error(monkeypatch, exe_a, caplog):
    """Unknown exception classes keep the full stacktrace -- that's a real bug signal."""
    import logging

    import chess.engine

    from sturddle_view.engines import probe_engine

    async def boom(*_a, **_kw):
        raise RuntimeError("something nobody expected")

    monkeypatch.setattr(chess.engine, "popen_uci", boom)
    with caplog.at_level(logging.DEBUG, logger="sturddle_view.engines"):
        await probe_engine(exe_a)
    error_records = [
        r for r in caplog.records
        if r.name == "sturddle_view.engines" and r.levelno >= logging.ERROR
    ]
    assert error_records, "unclassified failure should log at ERROR with traceback"
    assert any(r.exc_info for r in error_records)


async def test_probe_engine_times_out_on_hanging_handshake(tmp_path, monkeypatch):
    """A spawned process that never replies to ``uci`` must not block the probe.

    Without a handshake timeout, ``GET /engines`` would hang forever on any
    binary that opens stdin and waits silently (e.g. python.exe).
    """
    import time

    from sturddle_view.engines import probe_engine

    # Fast bound so the test stays snappy; classification is what matters.
    monkeypatch.setenv("SV_ENGINE_PROBE_TIMEOUT_SEC", "0.1")

    # A tiny "engine" that opens stdin and never replies to anything.
    py = tmp_path / "hang.py"
    py.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n")
    if sys.platform.startswith("win"):
        wrapper = tmp_path / "hang.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        exe = str(wrapper)
    else:
        py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        exe = str(py)

    t0 = time.monotonic()
    _name, schema, error = await probe_engine(exe)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"probe should time out fast, took {elapsed:.2f}s"
    assert schema == {}
    assert isinstance(error, dict)
    assert error["code"] == "engine_not_uci"


async def test_probe_engine_classifies_non_uci_engine(monkeypatch, exe_a):
    """A spawned process that doesn't speak UCI maps to engine_not_uci."""
    import chess.engine

    from sturddle_view.engines import probe_engine

    async def boom(*_a, **_kw):
        raise chess.engine.EngineError("did not respond to uci")

    monkeypatch.setattr(chess.engine, "popen_uci", boom)
    _name, _schema, error = await probe_engine(exe_a)
    assert isinstance(error, dict)
    assert error["code"] == "engine_not_uci"


# -- engine lock tests --------------------------------------------------------

def _make_client_with_tourney(tmp_path, exe_path, status):
    """Return (client, eid) with one tournament at `status` referencing engine E."""
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    c = TestClient(app)
    c.headers["Authorization"] = "Bearer test-token"
    eid = c.post("/engines", json={"name": "E", "path": exe_path}).json()["id"]
    store = TournamentStore(tmp_path / "tourneys")
    t = store.create(
        name="T1",
        template={},
        engines=[{"id": eid, "name": "E", "cmd": exe_path}],
    )
    if status != t.status:
        store.update_status(t.id, status)
    app.state.tournament_store = store
    return c, eid


def test_locked_engine_delete_409(tmp_path, exe_a):
    c, eid = _make_client_with_tourney(tmp_path, exe_a, STATUS_RUNNING)
    r = c.delete(f"/engines/{eid}")
    assert r.status_code == 409
    assert "T1" in r.json()["detail"]


def test_locked_engine_patch_409(tmp_path, exe_a):
    c, eid = _make_client_with_tourney(tmp_path, exe_a, STATUS_RUNNING)
    r = c.patch(f"/engines/{eid}", json={"name": "E2"})
    assert r.status_code == 409
    assert "T1" in r.json()["detail"]


@pytest.mark.parametrize("status", [STATUS_IDLE, STATUS_STOPPED, STATUS_FAILED, STATUS_DONE])
def test_non_running_tourney_does_not_lock(tmp_path, exe_a, status):
    c, eid = _make_client_with_tourney(tmp_path, exe_a, status)
    assert c.delete(f"/engines/{eid}").status_code == 204


def test_locked_engine_refresh_schema_409(tmp_path, exe_a):
    c, eid = _make_client_with_tourney(tmp_path, exe_a, STATUS_RUNNING)
    r = c.post(f"/engines/{eid}/refresh-schema")
    assert r.status_code == 409
    assert "T1" in r.json()["detail"]


def test_locked_engine_get_includes_tourney_info(tmp_path, exe_a):
    c, eid = _make_client_with_tourney(tmp_path, exe_a, STATUS_RUNNING)
    body = c.get(f"/engines/{eid}").json()
    locked = body["locked"]
    assert len(locked) == 1
    assert locked[0]["name"] == "T1"
    assert locked[0]["status"] == STATUS_RUNNING


def test_add_rejects_when_probe_fails(client, monkeypatch, exe_a):
    """A failing probe rejects the add: 400 with structured detail, registry untouched."""
    async def fake_probe(_path, args=None, env=None):
        return None, {}, {"code": "engine_not_launchable", "message": "Could not launch engine (file is not a runnable program for this system)."}

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", fake_probe)
    r = client.post("/engines", json={"path": exe_a})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == "engine_not_launchable"
    assert detail["message"]
    # Registry untouched.
    listed = client.get("/engines").json()
    assert listed["engines"] == []
    assert listed["selected_id"] is None


def test_add_rejects_non_uci_engine(client, monkeypatch, exe_a):
    """Engine that spawns but does not speak UCI is rejected (not silently added)."""
    async def fake_probe(_path, args=None, env=None):
        return None, {}, {"code": "engine_not_uci", "message": "Engine did not respond as a UCI engine."}

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", fake_probe)
    r = client.post("/engines", json={"path": exe_a})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "engine_not_uci"
    assert client.get("/engines").json()["engines"] == []


def test_add_omits_probe_error_on_success(client, monkeypatch, exe_a):
    """The success response shape stays unchanged — no `probe_error` key when the probe worked."""
    async def fake_probe(_path, args=None, env=None):
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

    async def fake_probe(_path, args=None, env=None):
        return None, {}, {"code": "engine_probe_failed", "message": "Could not probe engine."}

    monkeypatch.setattr("sturddle_view.api.engines.probe_engine", fake_probe)
    r = client.post(f"/engines/{eid}/refresh-schema")
    assert r.status_code == 502
    assert "Could not probe engine" in r.json()["detail"]


# -- Launch profile (args + env) ---------------------------------------------

async def test_probe_engine_passes_args_and_env(tmp_path):
    """probe_engine spawns with the exact args/env it was given."""
    from sturddle_view.engines import probe_engine

    exe = _make_echo_uci(tmp_path / "echo")
    name, _schema, err = await probe_engine(
        exe, args=["--foo", "bar baz"], env={"SV_TEST_ENV": "hello"}
    )
    assert err is None, err
    # The fake engine reports argv via `id name`; python-chess concatenates
    # multiple `id name` lines but here we only emit one. argv[1:] joined
    # with '|' shows the args we passed (proxy-less direct probe).
    assert name == "argv:--foo|bar baz"


def test_add_persists_args_and_env(client, exe_a):
    r = client.post("/engines", json={
        "name": "E1",
        "path": exe_a,
        "args": ["--foo", "bar"],
        "env": {"K": "v"},
    })
    assert r.status_code == 201
    body = r.json()
    assert body["args"] == ["--foo", "bar"]
    assert body["env"] == {"K": "v"}
    listed = client.get("/engines").json()["engines"][0]
    assert listed["args"] == ["--foo", "bar"]
    assert listed["env"] == {"K": "v"}


def test_patch_updates_args_and_env(client, exe_a):
    eid = client.post("/engines", json={"name": "E1", "path": exe_a}).json()["id"]
    r = client.patch(f"/engines/{eid}", json={"args": ["-q"], "env": {"X": "1"}})
    assert r.status_code == 200
    assert r.json()["args"] == ["-q"]
    assert r.json()["env"] == {"X": "1"}


def test_args_and_env_validation(client, exe_a):
    # env key with '=' is rejected.
    r = client.post("/engines", json={"name": "E1", "path": exe_a, "env": {"K=Y": "v"}})
    assert r.status_code == 400
    # blank env key.
    r = client.post("/engines", json={"name": "E1", "path": exe_a, "env": {"": "v"}})
    assert r.status_code == 400
    # NUL in arg.
    r = client.post("/engines", json={"name": "E1", "path": exe_a, "args": ["a\x00b"]})
    assert r.status_code == 400


def test_probe_endpoint_uses_in_progress_profile(client, tmp_path):
    """POST /engines/probe spawns with the supplied args/env, no registry write."""
    exe = _make_echo_uci(tmp_path / "echo")
    r = client.post("/engines/probe", json={
        "path": exe,
        "args": ["--mode", "fast"],
        "env": {"SV_TEST_ENV": "probe-only"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["probe_error"] is None
    assert body["uci_name"] == "argv:--mode|fast"
    # Registry is untouched.
    assert client.get("/engines").json()["engines"] == []


def test_add_probes_with_supplied_args_and_env(client, tmp_path):
    """POST /engines uses the provided args/env when probing the new engine."""
    exe = _make_echo_uci(tmp_path / "echo")
    r = client.post("/engines", json={
        "name": "E1",
        "path": exe,
        "args": ["--probe-flag"],
        "env": {"SV_TEST_ENV": "added"},
    })
    assert r.status_code == 201
    # The argv reported back via UCI confirms the probe saw our args.
    listed = client.get("/engines").json()["engines"][0]
    # option_schema ends up empty (echo engine declares no options) — that's
    # fine, the assertion that matters is no probe error and persistence.
    assert listed["args"] == ["--probe-flag"]
    assert listed["env"] == {"SV_TEST_ENV": "added"}


def test_refresh_schema_uses_saved_args_and_env(client, tmp_path):
    """refresh-schema re-spawns with the saved launch profile."""
    exe = _make_echo_uci(tmp_path / "echo")
    eid = client.post("/engines", json={
        "name": "E1",
        "path": exe,
        "args": ["--saved"],
        "env": {"SV_TEST_ENV": "from-disk"},
    }).json()["id"]
    # The echo engine round-trips its argv via `id name`, so the persisted
    # uci_name proves the re-probe spawned with the saved profile.
    r = client.post(f"/engines/{eid}/refresh-schema")
    assert r.status_code == 200
    assert r.json()["uci_name"] == "argv:--saved"
    listed = client.get("/engines").json()["engines"][0]
    assert listed["id"] == eid
    assert listed["args"] == ["--saved"]
    assert listed["env"] == {"SV_TEST_ENV": "from-disk"}


def test_auth_required(tmp_path):
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        # No Authorization header.
        assert c.get("/engines").status_code == 401
        assert c.post("/engines", json={"name": "A", "path": "/p"}).status_code == 401
