"""Slice 5: REST + WS surface for tournaments.

Uses ``fastapi.TestClient`` with ``auth_disabled=True``. The actual
fastchess subprocess is replaced by a fake-fastchess script via a
``build_command`` monkeypatch (same trick as ``test_tournament_fastchess``).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.tournament import fastchess as fc_mod
from sturddle_view.tournament.fastchess import FastchessRunner


FAKE_FASTCHESS = r"""
import sys, time
i = 1
rc = 0
while i < len(sys.argv):
    a = sys.argv[i]
    if a == "--print":
        n = int(sys.argv[i+1]); i += 2
        for k in range(n):
            print(f"out {k}", flush=True)
    elif a == "--sleep":
        time.sleep(float(sys.argv[i+1])); i += 2
    elif a == "--exit":
        rc = int(sys.argv[i+1]); i += 2
    else:
        i += 1
sys.exit(rc)
"""


@pytest.fixture
def settings(tmp_path):
    s = Settings(auth_disabled=True)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = sys.executable  # always present in tests
    return s


@pytest.fixture
def client(settings, monkeypatch):
    # Ensure detect_binary returns whatever we configured (sys.executable).
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    app = create_app(settings=settings)
    with TestClient(app) as c:
        yield c


def _engines_payload() -> list[dict]:
    return [{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_list_empty(client):
    r = client.get("/api/tournaments")
    assert r.status_code == 200
    body = r.json()
    assert body["active_id"] is None
    assert body["tournaments"] == []


def test_create_returns_id_and_status_idle(client):
    r = client.post("/api/tournaments", json={
        "name": "first",
        "template": {"tc": "10+0.1"},
        "engines": _engines_payload(),
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "first"
    assert body["status"] == "idle"
    assert body["template"]["tc"] == "10+0.1"
    assert "seed" in body["template"]
    assert body["id"]


def test_create_rejects_missing_engines(client):
    r = client.post("/api/tournaments", json={"name": "x", "engines": []})
    assert r.status_code == 400


def test_create_rejects_single_engine(client):
    r = client.post("/api/tournaments", json={
        "name": "x",
        "engines": [{"name": "solo", "cmd": "/bin/x"}],
    })
    assert r.status_code == 400


def test_create_rejects_duplicate_name(client):
    payload = {"name": "dup", "engines": _engines_payload()}
    assert client.post("/api/tournaments", json=payload).status_code == 201
    r = client.post("/api/tournaments", json=payload)
    assert r.status_code == 409


def test_get_unknown_returns_404(client):
    r = client.get("/api/tournaments/does-not-exist")
    assert r.status_code == 404


def test_get_includes_standings_with_no_games(client):
    created = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()
    r = client.get(f"/api/tournaments/{created['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["standings"] == {"games": 0, "engines": []}


def test_list_returns_created(client):
    a = client.post("/api/tournaments", json={
        "name": "a", "engines": _engines_payload(),
    }).json()
    b = client.post("/api/tournaments", json={
        "name": "b", "engines": _engines_payload(),
    }).json()
    listed = client.get("/api/tournaments").json()
    ids = [t["id"] for t in listed["tournaments"]]
    assert a["id"] in ids and b["id"] in ids


def test_delete_removes(client):
    t = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()
    r = client.delete(f"/api/tournaments/{t['id']}")
    assert r.status_code == 204
    assert client.get(f"/api/tournaments/{t['id']}").status_code == 404


def test_delete_unknown_returns_404(client):
    r = client.delete("/api/tournaments/nope")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------


def _patch_fake_fastchess(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(
        fc_mod, "build_command",
        lambda spec: [sys.executable, "-c", FAKE_FASTCHESS, *args],
    )


def _wait_status(client, tid: str, want: str, timeout: float = 5.0) -> dict:
    """Poll GET /api/tournaments/{id} until status matches `want`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/tournaments/{tid}").json()
        if body["status"] == want:
            return body
        time.sleep(0.05)
    raise AssertionError(f"status never reached {want!r}; last={body}")


def test_start_then_done(client, monkeypatch):
    _patch_fake_fastchess(monkeypatch, "--print", "3", "--exit", "0")
    t = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()

    r = client.post(f"/api/tournaments/{t['id']}/start")
    assert r.status_code == 200
    assert r.json()["status"] == "running"

    final = _wait_status(client, t["id"], "done")
    assert final["status"] == "done"
    listed = client.get("/api/tournaments").json()
    assert listed["active_id"] is None


def test_start_unknown_returns_404(client):
    r = client.post("/api/tournaments/does-not-exist/start")
    assert r.status_code == 404


def test_start_rejects_when_busy(client, monkeypatch):
    _patch_fake_fastchess(monkeypatch, "--sleep", "30")
    a = client.post("/api/tournaments", json={
        "name": "a", "engines": _engines_payload(),
    }).json()
    b = client.post("/api/tournaments", json={
        "name": "b", "engines": _engines_payload(),
    }).json()

    assert client.post(f"/api/tournaments/{a['id']}/start").status_code == 200
    try:
        r = client.post(f"/api/tournaments/{b['id']}/start")
        assert r.status_code == 409
    finally:
        client.post(f"/api/tournaments/{a['id']}/stop")
        _wait_status(client, a["id"], "stopped")


def test_stop_active(client, monkeypatch):
    _patch_fake_fastchess(monkeypatch, "--sleep", "30")
    t = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()
    client.post(f"/api/tournaments/{t['id']}/start")
    r = client.post(f"/api/tournaments/{t['id']}/stop")
    assert r.status_code == 200
    final = _wait_status(client, t["id"], "stopped")
    assert final["status"] == "stopped"


def test_stop_unknown_returns_404(client):
    r = client.post("/api/tournaments/nope/stop")
    assert r.status_code == 404


def test_delete_running_rejects_409(client, monkeypatch):
    _patch_fake_fastchess(monkeypatch, "--sleep", "30")
    t = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()
    client.post(f"/api/tournaments/{t['id']}/start")
    try:
        r = client.delete(f"/api/tournaments/{t['id']}")
        assert r.status_code == 409
    finally:
        client.post(f"/api/tournaments/{t['id']}/stop")
        _wait_status(client, t["id"], "stopped")


def test_start_with_missing_binary_returns_400(client, settings, monkeypatch):
    # Override detect_binary to None inside this test
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: None),
    )
    t = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()
    r = client.post(f"/api/tournaments/{t['id']}/start")
    assert r.status_code == 400
    # Tournament should be left in a STOPPED state due to start rollback
    assert client.get(f"/api/tournaments/{t['id']}").json()["status"] == "stopped"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_get_settings(client):
    r = client.get("/api/tournament-settings")
    assert r.status_code == 200
    body = r.json()
    assert "fastchess_path" in body
    assert "tournaments_root" in body
    assert "default_template" in body
    assert "fastchess_detected" in body


def test_put_settings_updates(client, tmp_path):
    new_root = str(tmp_path / "alt-root")
    r = client.put("/api/tournament-settings", json={
        "tournaments_root": new_root,
        "default_template": {"tc": "60+0.6"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["tournaments_root"] == new_root
    assert body["default_template"] == {"tc": "60+0.6"}


def test_settings_persist_across_restart(tmp_path, monkeypatch):
    """After PUT, a fresh app constructed from the same settings file
    should read back the persisted values."""
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    s1 = Settings(auth_disabled=True)
    s1.tournament_root = str(tmp_path / "tournaments")
    s1.tournament_fastchess_path = sys.executable
    app1 = create_app(settings=s1)
    with TestClient(app1) as c1:
        c1.put("/api/tournament-settings", json={
            "default_template": {"tc": "60+0.6", "games_in_parallel": 2},
        })

    # Fresh app, fresh Settings — auto-loads from the persisted file
    s2 = Settings(auth_disabled=True)
    s2.apply_persisted()
    app2 = create_app(settings=s2)
    with TestClient(app2) as c2:
        body = c2.get("/api/tournament-settings").json()
        assert body["default_template"] == {"tc": "60+0.6", "games_in_parallel": 2}


# ---------------------------------------------------------------------------
# Reconciliation on app boot
# ---------------------------------------------------------------------------


def test_reconcile_marks_stale_running_as_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    # Boot an app, create+start a long-running tournament, then drop the
    # app without stopping (simulating a server crash). The next app
    # construction must reconcile the on-disk 'running' to 'stopped'.
    s = Settings(auth_disabled=True)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = sys.executable
    monkeypatch.setattr(
        fc_mod, "build_command",
        lambda spec: [sys.executable, "-c", FAKE_FASTCHESS, "--sleep", "30"],
    )

    app1 = create_app(settings=s)
    with TestClient(app1) as c1:
        t = c1.post("/api/tournaments", json={
            "name": "x", "engines": _engines_payload(),
        }).json()
        c1.post(f"/api/tournaments/{t['id']}/start")
        # Confirm running
        assert c1.get(f"/api/tournaments/{t['id']}").json()["status"] == "running"
        # Forcibly mark on-disk to running but never stop -- TestClient
        # context manager runs lifespan shutdown which would clean up.
        # We need to bypass that to simulate a *crash*. Easiest: directly
        # write state.json with status=running to mimic the post-crash state.
        # But since /start already set status=running on disk, we just don't
        # let the app shutdown stop it. The lifespan shutdown WILL stop the
        # tournament gracefully on context exit, defeating the test.
        # Workaround: end the TestClient WITHOUT triggering lifespan
        # shutdown by using __enter__ / __exit__ manually -- but TestClient
        # already ran lifespan. So instead: stop the tournament normally,
        # then re-mark its on-disk state to running by hand to simulate the
        # crash state.
        c1.post(f"/api/tournaments/{t['id']}/stop")

    # Manually corrupt the on-disk state to "running" to simulate the crash
    state_path = Path(s.tournament_root) / t["id"] / "state.json"
    import json as _json
    state = _json.loads(state_path.read_text())
    state["status"] = "running"
    state_path.write_text(_json.dumps(state))

    # Boot a fresh app -- reconcile should flip it back to stopped
    app2 = create_app(settings=s)
    with TestClient(app2) as c2:
        body = c2.get(f"/api/tournaments/{t['id']}").json()
        assert body["status"] == "stopped"


# ---------------------------------------------------------------------------
# WebSocket: tournament events flow through the EventBus to /ws
# ---------------------------------------------------------------------------


def test_ws_receives_tournament_status_change(client, monkeypatch):
    _patch_fake_fastchess(monkeypatch, "--exit", "0")
    t = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()

    # Open WS first so we don't miss the events.
    with client.websocket_connect("/ws?token=") as ws:
        client.post(f"/api/tournaments/{t['id']}/start")

        seen_kinds: list[str] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                msg = ws.receive_json(mode="text")
            except Exception:
                break
            seen_kinds.append(msg.get("kind"))
            # Stop once we've seen at least one tournament_status with status=done
            payload = msg.get("payload", {})
            if msg.get("kind") == "tournament_status" and payload.get("status") == "done":
                break

    assert "tournament_status" in seen_kinds
