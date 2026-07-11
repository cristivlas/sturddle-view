"""REST + WS surface for tournaments.

Uses ``fastapi.TestClient`` with ``auth_disabled=True``. The actual
fastchess subprocess is replaced by a fake-fastchess script via a
``build_command`` monkeypatch (same trick as ``test_tournament_fastchess``).
"""
from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sturddle_view.api.tournaments import _snap_terminal_sprt_verdict
from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.tournament import fastchess as fc_mod
from sturddle_view.tournament.fastchess import FastchessRunner
from sturddle_view.tournament.pgn_stats import SPRT_CONTINUE, SPRT_H0, SPRT_H1
from sturddle_view.tournament.store import (
    STATUS_DONE,
    STATUS_RUNNING,
    STATUS_STOPPED,
    TournamentStore,
)


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
def client(settings, monkeypatch, tmp_path):
    # Ensure detect_binary returns whatever we configured (sys.executable).
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    # Isolated registry: rating resolution reads it during standings
    # serialization; the default would lazily load the user's real file.
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c


def _engines_payload() -> list[dict]:
    return [{"id": "id-A", "name": "A", "cmd": "/bin/A"}, {"id": "id-B", "name": "B", "cmd": "/bin/B"}]


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


def test_create_injects_oversubscribe_from_env(client, monkeypatch):
    from sturddle_view.api.tournaments import ALLOW_OVERSUBSCRIBE_ENV

    monkeypatch.setenv(ALLOW_OVERSUBSCRIBE_ENV, "1")
    r = client.post("/api/tournaments", json={
        "name": "over", "template": {"tc": "10+0.1"}, "engines": _engines_payload(),
    })
    assert r.status_code == 201, r.text
    assert r.json()["template"]["allow_oversubscribe"] is True


def test_create_no_oversubscribe_without_env(client):
    r = client.post("/api/tournaments", json={
        "name": "plain", "template": {"tc": "10+0.1"}, "engines": _engines_payload(),
    })
    assert r.status_code == 201, r.text
    assert "allow_oversubscribe" not in r.json()["template"]


def test_create_rejects_missing_engines(client):
    r = client.post("/api/tournaments", json={"name": "x", "engines": []})
    assert r.status_code == 400


def test_create_rejects_single_engine(client):
    r = client.post("/api/tournaments", json={
        "name": "x",
        "engines": [{"id": "id-solo", "name": "solo", "cmd": "/bin/x"}],
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
    assert body["standings"]["games"] == 0
    assert body["standings"]["engines"] == []


_PGN_TWO_GAMES = (
    "[Event \"x\"]\n[White \"A\"]\n[Black \"B\"]\n[Result \"1-0\"]\n\n1-0\n\n"
    "[Event \"x\"]\n[White \"B\"]\n[Black \"A\"]\n[Result \"0-1\"]\n\n0-1\n\n"
)


def test_get_standings_games_from_pgn(client, settings):
    """``standings.games`` is PGN-derived (compute_standings). We never
    resume across stop cycles, so the PGN never accumulates duplicate
    replays and its game count is ground truth."""
    created = client.post("/api/tournaments", json={
        "name": "y", "engines": _engines_payload(),
    }).json()
    tid = created["id"]
    pgn = Path(settings.tournament_root) / tid / "games.pgn"
    pgn.parent.mkdir(parents=True, exist_ok=True)
    pgn.write_text(_PGN_TWO_GAMES, encoding="utf-8")
    body = client.get(f"/api/tournaments/{tid}").json()
    assert body["standings"]["games"] == 2


_PGN_BALANCED = (
    "[Event \"x\"]\n[White \"A\"]\n[Black \"B\"]\n[Result \"1-0\"]\n\n1-0\n\n"
    "[Event \"x\"]\n[White \"B\"]\n[Black \"A\"]\n[Result \"1-0\"]\n\n1-0\n\n"
)


def test_standings_anchor_live_first_frozen_fallback(client, settings):
    """Rating resolution: create freezes the registry rating into the
    ref; standings prefer the live value (edits re-anchor a finished
    tournament); a deleted engine falls back to its frozen snapshot."""
    reg = client.app.state.engines
    a = reg.add(name="A", path="/bin/A", rating=3000)
    b = reg.add(name="B", path="/bin/B")
    created = client.post("/api/tournaments", json={
        "name": "anchored",
        "engines": [
            {"id": a.id, "name": "A", "cmd": "/bin/A"},
            {"id": b.id, "name": "B", "cmd": "/bin/B"},
        ],
    }).json()
    tid = created["id"]
    assert created["engines"][0]["rating"] == 3000
    assert "rating" not in created["engines"][1]

    pgn = Path(settings.tournament_root) / tid / "games.pgn"
    pgn.parent.mkdir(parents=True, exist_ok=True)
    pgn.write_text(_PGN_BALANCED, encoding="utf-8")

    def anchored():
        body = client.get(f"/api/tournaments/{tid}").json()
        return {e["name"]: e["elo_anchored"] for e in body["standings"]["engines"]}

    # Balanced 1-1 -> elo_ordo 0 for both -> anchored == A's rating.
    assert anchored() == {"A": pytest.approx(3000), "B": pytest.approx(3000)}
    # Live registry edit re-anchors on next read; nothing frozen consulted.
    reg.update(a.id, rating=3200)
    assert anchored()["A"] == pytest.approx(3200)
    # Deleted engine: frozen snapshot (3000) is all we have.
    reg.remove(a.id)
    assert anchored()["A"] == pytest.approx(3000)


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


# ---------------------------------------------------------------------------
# /start wipe-required gate (universal: all engine counts)
# ---------------------------------------------------------------------------


def test_start_requires_confirm_wipe_when_stopped(client, settings, monkeypatch):
    """status=STOPPED -> 409 with reason=wipe_required when confirm_wipe is
    omitted. Stop wipes on next Start; a silent /start would destroy
    data."""
    _patch_fake_fastchess(monkeypatch, "--exit", "0")
    t = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()
    store = TournamentStore(Path(settings.tournament_root))
    store.update_status(t["id"], "stopped", stopped_at="2026-01-01T00:00:00+00:00")
    # Drop a stray file we expect to survive (no wipe happens on 409).
    pgn = Path(settings.tournament_root) / t["id"] / "games.pgn"
    pgn.parent.mkdir(parents=True, exist_ok=True)
    pgn.write_text("[Result \"1-0\"]\n", encoding="utf-8")

    r = client.post(f"/api/tournaments/{t['id']}/start")
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "wipe_required"
    assert pgn.exists(), "no wipe should happen on the 409 path"


def test_start_with_confirm_wipe_wipes_and_starts(client, settings, monkeypatch):
    """status=STOPPED + confirm_wipe=true -> wipes the dir and starts
    the tournament fresh."""
    _patch_fake_fastchess(monkeypatch, "--print", "1", "--exit", "0")
    t = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()
    store = TournamentStore(Path(settings.tournament_root))
    store.update_status(t["id"], "stopped", stopped_at="2026-01-01T00:00:00+00:00")
    pgn = Path(settings.tournament_root) / t["id"] / "games.pgn"
    pgn.parent.mkdir(parents=True, exist_ok=True)
    pgn.write_text("[Result \"1-0\"]\n", encoding="utf-8")

    r = client.post(f"/api/tournaments/{t['id']}/start?confirm_wipe=true")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert not pgn.exists(), "wipe should remove the stray PGN"


def test_start_idle_does_not_require_confirm_wipe(client, monkeypatch):
    """status=IDLE (fresh tournament) -> /start proceeds without flag."""
    _patch_fake_fastchess(monkeypatch, "--exit", "0")
    t = client.post("/api/tournaments", json={
        "name": "x", "engines": _engines_payload(),
    }).json()
    r = client.post(f"/api/tournaments/{t['id']}/start")
    assert r.status_code == 200


def test_wipe_for_restart_preserves_immutables(settings):
    """``wipe_for_restart`` keeps name/template/engines/engine_defaults
    intact, resets runtime state (status, last_error)."""
    store = TournamentStore(Path(settings.tournament_root))
    t = store.create(
        name="orig",
        template={"foo": "bar"},
        engines=_engines_payload(),
        engine_defaults={"threads": 4},
    )
    store.update_status(t.id, "failed", last_error={"rc": 1})
    pgn = Path(settings.tournament_root) / t.id / "games.pgn"
    pgn.write_text("[Result \"1-0\"]\n", encoding="utf-8")

    pre_template = dict(t.template)
    pre_engines = list(t.engines)
    pre_defaults = dict(t.engine_defaults)

    after = store.wipe_for_restart(t.id)
    assert after.name == "orig"
    assert after.template == pre_template
    assert after.engines == pre_engines
    assert after.engine_defaults == pre_defaults
    assert after.status == "idle"
    assert after.last_error is None
    assert not pgn.exists()


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
    assert "engine_default_syzygy_path" in body
    # Book defaults are surfaced so the tourney form can seed from Common.
    assert "engine_default_book_path" in body
    assert "engine_default_book_plies" in body
    assert "engine_default_book_order" in body


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


def test_reconcile_marks_stale_running_as_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    # Boot an app, create+start a long-running tournament, then drop the
    # app without stopping (simulating a server crash). The next app
    # construction must reconcile the on-disk 'running' to 'failed'.
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

    # Boot a fresh app -- reconcile should flip it to failed with a
    # synthetic last_error so the UI surfaces *why*.
    app2 = create_app(settings=s)
    with TestClient(app2) as c2:
        body = c2.get(f"/api/tournaments/{t['id']}").json()
        assert body["status"] == "failed"
        assert body["last_error"] is not None
        assert "Server was killed" in body["last_error"]["stderr_tail"][0]


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


# ---------------------------------------------------------------------------
# _resolve_sprt: SPRT bool -> full params dict at create/edit time
# ---------------------------------------------------------------------------


@pytest.fixture
def sprt_settings(tmp_path, monkeypatch):
    s = Settings(auth_disabled=True)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = sys.executable
    s.tournament_sprt_defaults = {"elo0": 3, "elo1": 15, "alpha": 0.02, "beta": 0.02}
    monkeypatch.setattr(FastchessRunner, "detect_binary", staticmethod(lambda c: c))
    return s


@pytest.fixture
def sprt_client(sprt_settings):
    app = create_app(settings=sprt_settings)
    with TestClient(app) as c:
        yield c


def test_create_sprt_true_merges_defaults(sprt_client):
    r = sprt_client.post("/api/tournaments", json={
        "name": "s", "template": {"tc": "5+0.05", "sprt": True},
        "engines": _engines_payload(),
    })
    assert r.status_code == 201, r.text
    sprt = r.json()["template"]["sprt"]
    assert sprt["elo0"] == 3
    assert sprt["elo1"] == 15
    assert sprt["alpha"] == 0.02
    assert sprt["beta"] == 0.02


def test_create_sprt_dict_overrides_defaults(sprt_client):
    r = sprt_client.post("/api/tournaments", json={
        "name": "s2", "template": {"tc": "5+0.05", "sprt": {"elo0": 7, "elo1": 20}},
        "engines": _engines_payload(),
    })
    assert r.status_code == 201, r.text
    sprt = r.json()["template"]["sprt"]
    assert sprt["elo0"] == 7
    assert sprt["elo1"] == 20
    # Remaining keys fall through from sprt_defaults.
    assert sprt["beta"] == 0.02


def test_create_no_sprt_passthrough(sprt_client):
    r = sprt_client.post("/api/tournaments", json={
        "name": "s3", "template": {"tc": "5+0.05", "rounds": 10},
        "engines": _engines_payload(),
    })
    assert r.status_code == 201, r.text
    assert "sprt" not in r.json()["template"]


def test_edit_sprt_true_merges_defaults(sprt_client):
    t = sprt_client.post("/api/tournaments", json={
        "name": "e1", "template": {"tc": "5+0.05"}, "engines": _engines_payload(),
    }).json()
    r = sprt_client.patch(f"/api/tournaments/{t['id']}", json={
        "name": "e1", "template": {"tc": "5+0.05", "sprt": True},
        "engines": _engines_payload(),
    })
    assert r.status_code == 200, r.text
    sprt = r.json()["template"]["sprt"]
    assert sprt["elo0"] == 3
    assert sprt["beta"] == 0.02

# ---------------------------------------------------------------------------
# engine_defaults snapshot at create time
# ---------------------------------------------------------------------------


def test_create_snapshots_engine_defaults_from_settings(client, settings):
    settings.engine_default_threads = 4
    settings.engine_default_hash_mb = 256
    settings.engine_default_syzygy_path = "/tb/syzygy"
    settings.engine_default_book_path = "/books/8moves.pgn"
    settings.engine_default_book_plies = 12
    settings.engine_default_book_order = "random"

    body = client.post("/api/tournaments", json={
        "name": "snap", "engines": _engines_payload(),
    }).json()

    ed = body["engine_defaults"]
    assert ed == {
        "threads": 4,
        "hash_mb": 256,
        "syzygy_path": "/tb/syzygy",
        "book_path": "/books/8moves.pgn",
        "book_plies": 12,
        "book_order": "random",
    }


def test_create_freezes_unset_engine_defaults_as_none(client, settings):
    # Settings has nothing set → snapshot still records every field
    # (with None values) so later Settings changes can't leak in.
    body = client.post("/api/tournaments", json={
        "name": "bare", "engines": _engines_payload(),
    }).json()
    assert body["engine_defaults"] == {
        "threads": None, "hash_mb": None, "syzygy_path": None,
        "book_path": None, "book_plies": None, "book_order": None,
    }


def test_create_book_from_template_overrides_settings(client, settings):
    # Book fields are per-tournament: the template payload wins over Common
    # settings. Non-book defaults (threads/hash/syzygy) still freeze from
    # settings.
    settings.engine_default_threads = 4
    settings.engine_default_syzygy_path = "/tb/syzygy"
    settings.engine_default_book_path = "/common/book.pgn"
    settings.engine_default_book_plies = 12
    settings.engine_default_book_order = "sequential"

    body = client.post("/api/tournaments", json={
        "name": "bookover", "engines": _engines_payload(),
        "template": {
            "tc": "10+0.1", "rounds": 4,
            "book_path": "/tourney/book.epd", "book_plies": 6, "book_order": "random",
        },
    }).json()

    ed = body["engine_defaults"]
    assert ed["book_path"] == "/tourney/book.epd"
    assert ed["book_plies"] == 6
    assert ed["book_order"] == "random"
    # Non-book defaults remain frozen from settings.
    assert ed["threads"] == 4
    assert ed["syzygy_path"] == "/tb/syzygy"


def test_create_book_falls_back_to_settings_when_template_omits(client, settings):
    # Template without book keys -> Common settings still seed the snapshot.
    settings.engine_default_book_path = "/common/book.pgn"
    settings.engine_default_book_plies = 12
    settings.engine_default_book_order = "sequential"

    body = client.post("/api/tournaments", json={
        "name": "bookfallback", "engines": _engines_payload(),
        "template": {"tc": "10+0.1", "rounds": 4},
    }).json()

    ed = body["engine_defaults"]
    assert ed["book_path"] == "/common/book.pgn"
    assert ed["book_plies"] == 12
    assert ed["book_order"] == "sequential"


def test_edit_book_from_template_overrides_settings(client, settings):
    settings.engine_default_book_path = "/common/book.pgn"
    created = client.post("/api/tournaments", json={
        "name": "edit-book", "engines": _engines_payload(),
        "template": {"tc": "10+0.1", "rounds": 4},
    }).json()
    assert created["engine_defaults"]["book_path"] == "/common/book.pgn"

    edited = client.patch(f"/api/tournaments/{created['id']}", json={
        "name": "edit-book", "engines": _engines_payload(),
        "template": {"tc": "10+0.1", "rounds": 4, "book_path": "/new/book.epd"},
    }).json()
    assert edited["engine_defaults"]["book_path"] == "/new/book.epd"


def test_create_empty_book_path_turns_book_off(client, settings):
    # An explicit empty book_path overrides Common: no book, and dependent
    # depth/order are cleared too (they'd be meaningless without a path).
    settings.engine_default_book_path = "/common/book.pgn"
    settings.engine_default_book_plies = 8
    settings.engine_default_book_order = "random"

    body = client.post("/api/tournaments", json={
        "name": "nobook", "engines": _engines_payload(),
        "template": {"tc": "10+0.1", "rounds": 4, "book_path": ""},
    }).json()

    ed = body["engine_defaults"]
    assert ed["book_path"] is None
    assert ed["book_plies"] is None
    assert ed["book_order"] is None


def test_edit_empty_book_path_turns_book_off(client, settings):
    # Editing a book-configured tournament to an empty path removes the book;
    # Common must NOT leak back in.
    settings.engine_default_book_path = "/common/book.pgn"
    created = client.post("/api/tournaments", json={
        "name": "edit-off", "engines": _engines_payload(),
        "template": {"tc": "10+0.1", "rounds": 4, "book_path": "/t/book.epd", "book_plies": 6},
    }).json()
    assert created["engine_defaults"]["book_path"] == "/t/book.epd"

    edited = client.patch(f"/api/tournaments/{created['id']}", json={
        "name": "edit-off", "engines": _engines_payload(),
        "template": {"tc": "10+0.1", "rounds": 4, "book_path": ""},
    }).json()
    assert edited["engine_defaults"]["book_path"] is None
    assert edited["engine_defaults"]["book_plies"] is None


def test_book_depth_and_order_round_trip(client, settings):
    body = client.post("/api/tournaments", json={
        "name": "book-rt", "engines": _engines_payload(),
        "template": {
            "tc": "10+0.1", "rounds": 4,
            "book_path": "/t/b.epd", "book_plies": 10, "book_order": "sequential",
        },
    }).json()
    ed = body["engine_defaults"]
    assert (ed["book_path"], ed["book_plies"], ed["book_order"]) == \
        ("/t/b.epd", 10, "sequential")


def test_duplicate_carries_book(client, settings):
    # Duplicate posts a new tournament from the source's frozen values; the
    # book snapshot must ride along (client re-sends it in the template).
    src = client.post("/api/tournaments", json={
        "name": "orig", "engines": _engines_payload(),
        "template": {"tc": "10+0.1", "rounds": 4, "book_path": "/t/b.epd", "book_plies": 7},
    }).json()
    dup = client.post("/api/tournaments", json={
        "name": "orig (copy)", "engines": _engines_payload(),
        "template": {"tc": "10+0.1", "rounds": 4, "book_path": "/t/b.epd", "book_plies": 7},
    }).json()
    assert dup["engine_defaults"]["book_path"] == "/t/b.epd"
    assert dup["engine_defaults"]["book_plies"] == 7
    assert src["engine_defaults"]["book_path"] == dup["engine_defaults"]["book_path"]


def test_create_snapshots_engine_options_from_registry(client):
    reg = client.app.state.engines
    a = reg.add(
        name="A", path="/bin/A",
        options={"WeightsFile": "/w/a.binx", "OwnBook": False},
    )
    body = client.post("/api/tournaments", json={
        "name": "opts", "engines": [
            {"id": a.id, "name": "A", "cmd": "/bin/A"},
            {"id": "id-B", "name": "B", "cmd": "/bin/B"},
        ],
    }).json()
    by_name = {e["name"]: e for e in body["engines"]}
    assert by_name["A"]["options"] == {"WeightsFile": "/w/a.binx", "OwnBook": False}
    # Engine not in the registry (direct API use) -> no options snapshot.
    assert "options" not in by_name["B"]


def test_create_omits_empty_options_snapshot(client):
    # Registered engine with no option overrides: key absent, same as an
    # unregistered engine -- {} vs missing must not encode registry state.
    reg = client.app.state.engines
    a = reg.add(name="A", path="/bin/A")
    body = client.post("/api/tournaments", json={
        "name": "noopts", "engines": [
            {"id": a.id, "name": "A", "cmd": "/bin/A"},
            {"id": "id-B", "name": "B", "cmd": "/bin/B"},
        ],
    }).json()
    assert all("options" not in e for e in body["engines"])


# ---------------------------------------------------------------------------
# PATCH /api/tournaments/{id}  (edit)
# ---------------------------------------------------------------------------


def _create(client, name="x") -> dict:
    return client.post("/api/tournaments", json={
        "name": name, "engines": _engines_payload(),
    }).json()


def test_patch_resnapshots_engine_options(client):
    reg = client.app.state.engines
    a = reg.add(name="A", path="/bin/A", options={"WeightsFile": "/w/v1.binx"})
    payload = {
        "name": "resnap", "engines": [
            {"id": a.id, "name": "A", "cmd": "/bin/A"},
            {"id": "id-B", "name": "B", "cmd": "/bin/B"},
        ],
    }
    t = client.post("/api/tournaments", json=payload).json()
    reg.update(a.id, options={"WeightsFile": "/w/v2.binx"})
    body = client.patch(f"/api/tournaments/{t['id']}", json=payload).json()
    by_name = {e["name"]: e for e in body["engines"]}
    assert by_name["A"]["options"] == {"WeightsFile": "/w/v2.binx"}


def test_patch_preserves_options_when_engine_deleted(client):
    # Engine removed from the registry after create: an edit that
    # round-trips the stored refs must keep the frozen snapshot rather
    # than silently dropping it.
    reg = client.app.state.engines
    a = reg.add(name="A", path="/bin/A", options={"WeightsFile": "/w.binx"})
    t = client.post("/api/tournaments", json={
        "name": "keep", "engines": [
            {"id": a.id, "name": "A", "cmd": "/bin/A"},
            {"id": "id-B", "name": "B", "cmd": "/bin/B"},
        ],
    }).json()
    reg.remove(a.id)
    body = client.patch(f"/api/tournaments/{t['id']}", json={
        "name": "keep", "engines": t["engines"],
    }).json()
    by_name = {e["name"]: e for e in body["engines"]}
    assert by_name["A"]["options"] == {"WeightsFile": "/w.binx"}


def test_patch_updates_name_template_engines(client):
    t = _create(client)
    r = client.patch(f"/api/tournaments/{t['id']}", json={
        "name": "renamed",
        "template": {"tc": "5+0.05", "rounds": 5},
        "engines": [{"id": "id-C", "name": "C", "cmd": "/bin/C"}, {"id": "id-D", "name": "D", "cmd": "/bin/D"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "renamed"
    assert body["template"]["tc"] == "5+0.05"
    assert body["status"] == "idle"
    assert [e["name"] for e in body["engines"]] == ["C", "D"]


def test_patch_resets_status_to_idle(client):
    from sturddle_view.tournament.store import STATUS_STOPPED
    t = _create(client)
    client.app.state.tournament_store.update_status(t["id"], STATUS_STOPPED)

    r = client.patch(f"/api/tournaments/{t['id']}", json={
        "name": t["name"], "template": {}, "engines": _engines_payload(),
    })
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_patch_rejects_fewer_than_two_engines(client):
    t = _create(client)
    r = client.patch(f"/api/tournaments/{t['id']}", json={
        "name": t["name"],
        "engines": [{"id": "id-A", "name": "A", "cmd": "/bin/A"}],
    })
    assert r.status_code == 400


def test_patch_wipes_tournament_dir_contents(client):
    """Editing a tournament wipes every on-disk artifact (PGN, fastchess
    config + rotated backups, logs, strays) and leaves a fresh state.json.
    Past data was produced under potentially different conditions and
    must not leak into future runs."""
    t = _create(client)
    store = client.app.state.tournament_store
    pgn = store.pgn_path(t["id"])
    pgn.write_text("[Event \"?\"]\n\n1. e4 *\n")
    cfg = store.config_path(t["id"])
    cfg.write_text("{\"games\": 7}")
    cfg_bak = cfg.with_name(cfg.name + ".20260101-000000.bak.gz")
    cfg_bak.write_bytes(b"\x1f\x8b\x08\x00fake")
    logs = store.logs_dir(t["id"])
    logs.mkdir(exist_ok=True)
    (logs / "fastchess.log").write_text("info: ...")
    stray = store._dir(t["id"]) / "stray.tmp"
    stray.write_text("leftover")

    r = client.patch(f"/api/tournaments/{t['id']}", json={
        "name": t["name"],
        "template": {"tc": "5+0"},
        "engines": _engines_payload(),
    })
    assert r.status_code == 200
    assert not pgn.exists()
    assert not cfg.exists()
    assert not cfg_bak.exists()
    assert not logs.exists()
    assert not stray.exists()
    # The freshly written state.json must be there and parseable.
    assert store._state_path(t["id"]).exists()


def test_patch_clears_orchestrator_event_history(client):
    """Stale events from the pre-edit run must not replay into post-edit
    live-window re-subscribes -- mirrors delete_tournament's behavior."""
    t = _create(client)
    orch = client.app.state.tournament_orch
    # Inject a synthetic event so we have something to clear.
    orch._event_history.setdefault(t["id"], deque()).append(
        {"kind": "synthetic", "tournament_id": t["id"]}
    )
    assert len(orch.event_history(t["id"])) == 1

    r = client.patch(f"/api/tournaments/{t['id']}", json={
        "name": t["name"],
        "template": {"tc": "5+0"},
        "engines": _engines_payload(),
    })
    assert r.status_code == 200
    assert orch.event_history(t["id"]) == []


def test_patch_unknown_returns_404(client):
    r = client.patch("/api/tournaments/no-such-id", json={
        "name": "x", "engines": _engines_payload(),
    })
    assert r.status_code == 404


def test_patch_rejects_duplicate_name(client):
    _create(client, "alpha")
    b = _create(client, "beta")
    r = client.patch(f"/api/tournaments/{b['id']}", json={
        "name": "alpha", "engines": _engines_payload(),
    })
    assert r.status_code == 409


def test_patch_allows_same_name(client):
    t = _create(client, "same")
    r = client.patch(f"/api/tournaments/{t['id']}", json={
        "name": "same",
        "template": {"tc": "3+0"},
        "engines": _engines_payload(),
    })
    assert r.status_code == 200
    assert r.json()["template"]["tc"] == "3+0"


def test_patch_running_returns_409(client, monkeypatch):
    _patch_fake_fastchess(monkeypatch, "--sleep", "30")
    t = _create(client)
    client.post(f"/api/tournaments/{t['id']}/start")
    try:
        r = client.patch(f"/api/tournaments/{t['id']}", json={
            "name": t["name"], "engines": _engines_payload(),
        })
        assert r.status_code == 409
    finally:
        client.post(f"/api/tournaments/{t['id']}/stop")
        _wait_status(client, t["id"], "stopped")


# ---------------------------------------------------------------------------
# Per-game PGN fetch
# ---------------------------------------------------------------------------


_THREE_GAME_PGN = """[Event "g1"]
[White "A"]
[Black "B"]
[Result "1-0"]

1. e4 e5 1-0

[Event "g2"]
[White "B"]
[Black "A"]
[Result "0-1"]

1. d4 d5 0-1

[Event "g3"]
[White "A"]
[Black "B"]
[Result "1/2-1/2"]

1. c4 c5 1/2-1/2
"""


def test_get_game_pgn_returns_nth_game(client):
    t = _create(client)
    pgn = client.app.state.tournament_store.pgn_path(t["id"])
    pgn.parent.mkdir(parents=True, exist_ok=True)
    pgn.write_text(_THREE_GAME_PGN, encoding="utf-8")

    r = client.get(f"/api/tournaments/{t['id']}/games/2/pgn")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "0-1" in body["pgn"]
    assert "1. d4" in body["pgn"]
    assert "1. e4" not in body["pgn"]


def test_get_game_pgn_unknown_tournament_returns_404(client):
    r = client.get("/api/tournaments/no-such/games/1/pgn")
    assert r.status_code == 404


def test_get_game_pgn_no_file_returns_404(client):
    t = _create(client)
    r = client.get(f"/api/tournaments/{t['id']}/games/1/pgn")
    assert r.status_code == 404


def test_get_game_pgn_out_of_range_returns_404(client):
    t = _create(client)
    pgn = client.app.state.tournament_store.pgn_path(t["id"])
    pgn.parent.mkdir(parents=True, exist_ok=True)
    pgn.write_text(_THREE_GAME_PGN, encoding="utf-8")

    r = client.get(f"/api/tournaments/{t['id']}/games/99/pgn")
    assert r.status_code == 404


# Same shape as _THREE_GAME_PGN but the middle entry is in-flight (Result "*").
# pgn_tail counts only decisive games, so what we call "game 2" must skip the
# `*` and resolve to the third entry (1/2-1/2). Guards the read_game_pgn fix.
_PGN_WITH_INFLIGHT_MIDDLE = """[Event "g1"]
[White "A"]
[Black "B"]
[Result "1-0"]

1. e4 e5 1-0

[Event "g2-inflight"]
[White "B"]
[Black "A"]
[Result "*"]

1. d4 d5 *

[Event "g3"]
[White "A"]
[Black "B"]
[Result "1/2-1/2"]

1. c4 c5 1/2-1/2
"""


def test_get_game_pgn_skips_non_decisive(client):
    t = _create(client)
    pgn = client.app.state.tournament_store.pgn_path(t["id"])
    pgn.parent.mkdir(parents=True, exist_ok=True)
    pgn.write_text(_PGN_WITH_INFLIGHT_MIDDLE, encoding="utf-8")

    r = client.get(f"/api/tournaments/{t['id']}/games/2/pgn")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "1/2-1/2" in body["pgn"]
    assert "1. c4" in body["pgn"]


def test_get_game_pgn_zero_returns_422(client):
    # FastAPI's ge=1 path validator rejects with 422 (validation error).
    t = _create(client)
    r = client.get(f"/api/tournaments/{t['id']}/games/0/pgn")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Terminal SPRT verdict snapping. DONE always concluded (fastchess only ends a
# SPRT match on an accepted hypothesis), so snap to the nearer bound regardless
# of drift. STOPPED may be a mid-run abort, so snap only near a bound.
# ---------------------------------------------------------------------------


def _sprt(llr, status=SPRT_CONTINUE, lower=-2.94, upper=2.94):
    return {"llr": llr, "lower_bound": lower, "upper_bound": upper, "status": status}


def test_snap_done_positive_llr_becomes_h1():
    out = _snap_terminal_sprt_verdict(_sprt(2.93), STATUS_DONE)
    assert out["status"] == SPRT_H1


def test_snap_done_negative_llr_becomes_h0():
    # LLR -2.75, bound -2.94: 0.19 short -- DONE snaps anyway (it concluded).
    out = _snap_terminal_sprt_verdict(_sprt(-2.75), STATUS_DONE)
    assert out["status"] == SPRT_H0


def test_snap_done_snaps_even_near_midpoint():
    # DONE means concluded; lean decides the side, no tolerance gate.
    assert _snap_terminal_sprt_verdict(_sprt(0.4), STATUS_DONE)["status"] == SPRT_H1
    assert _snap_terminal_sprt_verdict(_sprt(-0.4), STATUS_DONE)["status"] == SPRT_H0


def test_snap_stopped_near_bound_snaps():
    out = _snap_terminal_sprt_verdict(_sprt(2.93), STATUS_STOPPED)
    assert out["status"] == SPRT_H1


def test_snap_stopped_mid_run_stays_continue():
    # A genuine mid-run abort far from both bounds is not invented away.
    out = _snap_terminal_sprt_verdict(_sprt(0.4), STATUS_STOPPED)
    assert out["status"] == SPRT_CONTINUE


def test_snap_running_never_snaps():
    out = _snap_terminal_sprt_verdict(_sprt(2.93), STATUS_RUNNING)
    assert out["status"] == SPRT_CONTINUE


def test_snap_leaves_already_concluded_untouched():
    out = _snap_terminal_sprt_verdict(_sprt(2.93, status=SPRT_H1), STATUS_DONE)
    assert out["status"] == SPRT_H1
