"""Slice 9b: /internal/proxy endpoint + per-proxy WS subscription.

Tests focus on the orchestrator's plumbing — using TestClient to drive
both the proxy ingest and a WS subscriber, with a fake tournament in
the running state."""
from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.tournament import fastchess as fc_mod
from sturddle_view.tournament.fastchess import FastchessRunner


FAKE_FASTCHESS = r"""
import sys, time
i = 1
while i < len(sys.argv):
    a = sys.argv[i]
    if a == "--sleep":
        time.sleep(float(sys.argv[i+1])); i += 2
    elif a == "--exit":
        sys.exit(int(sys.argv[i+1]))
    else:
        i += 1
"""


@pytest.fixture
def running_app(tmp_path, monkeypatch):
    """Boot an app with fastchess configured and a tournament started.
    The fake-fastchess just sleeps so the tournament stays 'running'
    long enough for the test."""
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    monkeypatch.setattr(
        fc_mod, "build_command",
        lambda spec: [sys.executable, "-c", FAKE_FASTCHESS, "--sleep", "30"],
    )

    s = Settings(auth_disabled=True)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = sys.executable
    app = create_app(settings=s)

    with TestClient(app) as c:
        # Create + start a tournament so the orchestrator has a secret.
        t = c.post("/api/tournaments", json={
            "name": "test",
            "engines": [{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
        }).json()
        c.post(f"/api/tournaments/{t['id']}/start")
        yield c, app
        # Tear down: stop the tournament so the lifespan exit is clean.
        c.post(f"/api/tournaments/{t['id']}/stop")


def test_internal_proxy_rejects_invalid_secret(running_app):
    client, _ = running_app
    r = client.post("/internal/proxy", json={
        "proxy_id": "p1",
        "secret": "bogus",
        "lines": ["position startpos"],
    })
    assert r.status_code == 401


def test_internal_proxy_accepts_valid_secret(running_app):
    client, app = running_app
    secret = app.state.tournament_orch.proxy_secret()
    assert secret  # tournament is running, so secret exists

    r = client.post("/internal/proxy", json={
        "proxy_id": "p1",
        "secret": secret,
        "engine_name": "EngineA",
        "lines": [],
    })
    assert r.status_code == 204


def test_internal_proxy_session_started_records_engine_name(running_app):
    client, app = running_app
    secret = app.state.tournament_orch.proxy_secret()

    # First post (engine_name + empty lines) registers the session.
    r = client.post("/internal/proxy", json={
        "proxy_id": "p1",
        "secret": secret,
        "engine_name": "EngineX",
        "lines": [],
    })
    assert r.status_code == 204
    orch = app.state.tournament_orch
    assert orch.engine_name_for("p1") == "EngineX"
    active = orch.active_proxies()
    assert {"proxy_id": "p1", "engine_name": "EngineX"} in active


def test_snapshot_replayed_on_late_subscribe(running_app):
    """A subscriber that connects after lines were ingested should
    receive the latest position/go/info as an instant snapshot — the
    fix for engines under long time control where the next live event
    can be ≥10s away."""
    client, app = running_app
    secret = app.state.tournament_orch.proxy_secret()

    # Ingest before subscribing.
    r = client.post("/internal/proxy", json={
        "proxy_id": "late",
        "secret": secret,
        "lines": [
            "position startpos moves e2e4 c7c5",
            "go wtime 100000 btime 100000",
            "info depth 12 score cp 25 pv g1f3 b8c6",
        ],
    })
    assert r.status_code == 204

    # Now connect: the snapshot should arrive on the very first
    # messages without any new lines being posted.
    with client.websocket_connect("/ws/tournament/proxy/late?token=") as ws:
        seen: list[str] = []
        for _ in range(3):
            msg = ws.receive_json(mode="text")
            seen.append(msg["line"])
        assert seen[0].startswith("position ")
        assert seen[1].startswith("go ")
        assert seen[2].startswith("info ")
        # The snapshot reflects the LATEST lines, not the entire history.
        assert "moves e2e4 c7c5" in seen[0]


def test_proxy_ws_subscriber_receives_lines(running_app):
    client, app = running_app
    secret = app.state.tournament_orch.proxy_secret()

    with client.websocket_connect("/ws/tournament/proxy/white?token=") as ws:
        # Post some lines from "white".
        client.post("/internal/proxy", json={
            "proxy_id": "white",
            "secret": secret,
            "lines": [
                "position startpos",
                "go wtime 5000 btime 5000",
                "info depth 1 score cp 30",
            ],
        })
        seen: list[str] = []
        for _ in range(3):
            msg = ws.receive_json(mode="text")
            assert msg["proxy_id"] == "white"
            seen.append(msg["line"])
        assert seen == [
            "position startpos",
            "go wtime 5000 btime 5000",
            "info depth 1 score cp 30",
        ]


def test_proxy_ws_subscriber_gets_ended_on_session_end(running_app):
    client, app = running_app
    secret = app.state.tournament_orch.proxy_secret()

    with client.websocket_connect("/ws/tournament/proxy/white?token=") as ws:
        # Post a line, then end the session.
        client.post("/internal/proxy", json={
            "proxy_id": "white",
            "secret": secret,
            "lines": ["position startpos"],
        })
        msg = ws.receive_json(mode="text")
        assert msg.get("line") == "position startpos"

        client.post("/internal/proxy", json={
            "proxy_id": "white",
            "secret": secret,
            "lines": [],
            "ended": True,
        })
        msg = ws.receive_json(mode="text")
        assert msg.get("ended") is True


def test_orchestrator_clears_secret_on_stop(tmp_path, monkeypatch):
    """After the tournament stops, the proxy_secret is cleared and
    posts are rejected."""
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    monkeypatch.setattr(
        fc_mod, "build_command",
        lambda spec: [sys.executable, "-c", FAKE_FASTCHESS, "--exit", "0"],
    )

    s = Settings(auth_disabled=True)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = sys.executable
    app = create_app(settings=s)

    with TestClient(app) as c:
        t = c.post("/api/tournaments", json={
            "name": "test",
            "engines": [{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
        }).json()
        c.post(f"/api/tournaments/{t['id']}/start")
        # Wait for fake-fastchess to exit cleanly → orchestrator clears secret.
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if app.state.tournament_orch.proxy_secret() is None:
                break
            time.sleep(0.05)

        # Post should now be rejected.
        r = c.post("/internal/proxy", json={
            "proxy_id": "p1",
            "secret": "anything",
            "lines": ["position startpos"],
        })
        assert r.status_code == 401
