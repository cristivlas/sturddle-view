"""Real-fastchess smoke test for Slice 9b.

Verifies that the wrapper + proxy + pair_index pipeline actually works
end-to-end with a real fastchess binary spawning real engine binaries.
Skipped if either fastchess or sturddle isn't available locally so this
file doesn't break CI on machines without them.

Per the project memory, fastchess is at ``~/Projects/fastchess/fastchess``
and sample engine binaries live under ``~/Projects/sturddle-2/dist/``.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings


FASTCHESS_PATH = Path.home() / "Projects" / "fastchess" / "fastchess"
ENGINE_CANDIDATES = [
    Path.home() / "Projects" / "sturddle-2" / "dist" / "sturddle-2.5.0-Linux-x86_64",
    Path.home() / "Projects" / "sturddle-2" / "dist" / "sturddle-2.4.0-Linux-x86_64",
]


def _have_real_setup() -> bool:
    if not FASTCHESS_PATH.is_file() or not os.access(FASTCHESS_PATH, os.X_OK):
        return False
    return all(p.is_file() and os.access(p, os.X_OK) for p in ENGINE_CANDIDATES)


pytestmark = pytest.mark.skipif(
    not _have_real_setup(),
    reason="real fastchess + sturddle not available on this machine",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_fastchess_proxy_pipeline_one_game(tmp_path):
    """Run a 1-game match with the real fastchess + 2 engines, with the
    proxy pipeline enabled. Verify the orchestrator's pair_index ends up
    with at least one paired game."""
    import uvicorn

    port = _free_port()
    s = Settings(token="test", auth_disabled=True, port=port)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = str(FASTCHESS_PATH)
    app = create_app(settings=s)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    assert server.started, "server failed to start"

    try:
        with TestClient(app) as client:
            # Quick TC; 1 round, 2 games (one each color).
            t = client.post("/api/tournaments", json={
                "name": "real-smoke",
                "template": {
                    "tc": "2+0.05",
                    "rounds": 1,
                    "games_in_parallel": 1,
                    "hash": 16,
                    "threads": 1,
                },
                "engines": [
                    {"name": "Sturddle 2.5.0",
                     "cmd": str(ENGINE_CANDIDATES[0])},
                    {"name": "Sturddle 2.4.0",
                     "cmd": str(ENGINE_CANDIDATES[1])},
                ],
            }).json()

            r = client.post(f"/api/tournaments/{t['id']}/start")
            assert r.status_code == 200, r.text

            # Sample the pair_index while the tournament is running so
            # we can prove the proxy pipeline is firing — once the
            # tournament ends, pair_index is reset.
            orch = app.state.tournament_orch
            pair_seen = False
            deadline = time.time() + 60
            while time.time() < deadline:
                if orch._pair_index.all_games():
                    pair_seen = True
                detail = client.get(f"/api/tournaments/{t['id']}").json()
                if detail["status"] in ("done", "stopped"):
                    break
                time.sleep(0.2)

            assert pair_seen, (
                "pair_index never recorded a pairing — proxy pipeline "
                "didn't reach the server"
            )

            final = client.get(f"/api/tournaments/{t['id']}").json()
            assert final["status"] in ("done", "stopped"), (
                f"unexpected final status: {final['status']}"
            )

            # PGN should have at least one finished game.
            pgn_path = Path(final.get("template", {}).get(
                "pgn_path", ""
            )) or (Path(s.tournament_root) / t["id"] / "games.pgn")
            # The pgn_path field isn't actually returned; just walk to it.
            actual_pgn = Path(s.tournament_root) / t["id"] / "games.pgn"
            assert actual_pgn.exists()
            assert actual_pgn.stat().st_size > 0

            # The pair_index will be empty NOW (cleared on terminal),
            # but during the run it should have observed at least one
            # game. Standings + games_list confirm that.
            assert final["games"], "no games in standings — pipeline likely broken"
            assert final["standings"]["games"] >= 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
