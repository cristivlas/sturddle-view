"""Pairing detection: ``info`` fan-out from the thinking engine to
subscribers of its opposite-color counterpart.

Two layers of tests:

  1. Pure unit tests against the orchestrator's pairing map — drive
     ``ingest_proxy_lines`` directly with synthetic UCI streams and
     inspect ``_pairing_map`` / ``_pairing_state``.
  2. End-to-end with two fake proxies posting to ``/internal/proxy``;
     a WS subscriber on proxy A receives proxy B's ``info`` lines
     tagged ``paired=True`` while B is thinking.
"""
from __future__ import annotations

import asyncio
import stat
import sys

import chess
import httpx
import pytest

from sturddle_view.tournament.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
)
from sturddle_view.tournament.runner import RunSpec
from sturddle_view.tournament.store import TournamentStore

from .conftest import run_uvicorn_subprocess


# ---------------------------------------------------------------------------
# Pure unit tests on the orchestrator's pairing map
# ---------------------------------------------------------------------------


class _FakeRunner:
    binary_path = "/fake/fastchess"
    def is_running(self) -> bool: return False
    async def start(self, spec: RunSpec, on_event) -> None: pass  # noqa: ARG002
    async def stop(self) -> None: pass


@pytest.fixture
def orch(tmp_path):
    store = TournamentStore(tmp_path / "tournaments")
    return Orchestrator(store, _FakeRunner())


def _board_after(*ucis: str) -> chess.Board:
    b = chess.Board()
    for u in ucis:
        b.push_uci(u)
    return b


@pytest.mark.asyncio
async def test_position_locks_color_and_registers(orch):
    """First ``position`` after a fresh state locks in the engine's
    color (side-to-move at that position) and registers it at the FEN."""
    await orch.ingest_proxy_lines("white", ["position startpos"])
    state = orch._pairing_state["white"]
    fen, color = state
    assert chess.Board(fen) == chess.Board()
    assert color == "white"
    assert orch._pairing_map[fen] == [("white", "white")]


@pytest.mark.asyncio
async def test_bestmove_advances_fen_keeps_color(orch):
    """``bestmove m`` re-registers the proxy at the post-move FEN.
    Engine color does NOT change — it's the engine's identity for
    the game; only the FEN advances."""
    await orch.ingest_proxy_lines("white", [
        "position startpos",
        "bestmove e2e4",
    ])
    fen, color = orch._pairing_state["white"]
    assert fen == _board_after("e2e4").fen()
    assert color == "white"


@pytest.mark.asyncio
async def test_two_proxies_join_at_same_fen_with_opposite_colors(orch):
    """Rendezvous: white plays e2e4 (registers waiting at fen-after-e4
    with color=white), black gets ``position`` for the same FEN
    (registers thinking with color=black). Same FEN, opposite colors."""
    await orch.ingest_proxy_lines("white", [
        "position startpos",
        "bestmove e2e4",
    ])
    await orch.ingest_proxy_lines("black", [
        "position startpos moves e2e4",
    ])
    fen_w, color_w = orch._pairing_state["white"]
    fen_b, color_b = orch._pairing_state["black"]
    assert fen_w == fen_b
    assert {color_w, color_b} == {"white", "black"}


@pytest.mark.asyncio
async def test_proxy_appears_in_map_at_most_once(orch):
    """Successive ``position`` lines transition the registration; the
    proxy never appears in more than one bucket."""
    await orch.ingest_proxy_lines("white", ["position startpos"])
    await orch.ingest_proxy_lines("white", [
        "position startpos moves e2e4 c7c5",
    ])
    occurrences = sum(
        1 for bucket in orch._pairing_map.values()
        for (pid, _s) in bucket if pid == "white"
    )
    assert occurrences == 1


@pytest.mark.asyncio
async def test_empty_bucket_is_pruned(orch):
    """When a transition empties a FEN bucket the dict entry is
    removed, keeping the map bounded."""
    await orch.ingest_proxy_lines("white", ["position startpos"])
    startpos = chess.Board().fen()
    assert startpos in orch._pairing_map
    await orch.ingest_proxy_lines("white", [
        "position startpos moves e2e4",
    ])
    assert startpos not in orch._pairing_map


@pytest.mark.asyncio
async def test_session_ended_unregisters(orch):
    await orch.ingest_proxy_lines("white", ["position startpos"])
    await orch.proxy_session_ended("white")
    assert "white" not in orch._pairing_state
    assert orch._pairing_map == {}


@pytest.mark.asyncio
async def test_ucinewgame_unregisters(orch):
    await orch.ingest_proxy_lines("white", ["position startpos"])
    await orch.ingest_proxy_lines("white", ["ucinewgame"])
    assert "white" not in orch._pairing_state
    assert orch._pairing_map == {}


@pytest.mark.asyncio
async def test_paired_subscribers_lookup(orch):
    """``_paired_subscribers`` returns the WS queues of the
    opposite-color proxy at the same FEN, excluding the caller."""
    fen = chess.Board().fen()
    orch._pairing_register("white", fen, "white")
    orch._pairing_register("black", fen, "black")

    q_white = orch.subscribe_to_proxy("white")
    q_black = orch.subscribe_to_proxy("black")

    paired = orch._paired_subscribers("white", fen, "white")
    assert q_black in paired
    assert q_white not in paired

    # Same color twice ⇒ no pairing (no opposite-color match).
    orch._pairing_unregister("black")
    orch._pairing_register("black", fen, "white")
    paired = orch._paired_subscribers("white", fen, "white")
    assert paired == set()


@pytest.mark.asyncio
async def test_info_fans_out_to_paired_subscriber(orch):
    """White is thinking at the post-e2e4-c7c5 FEN; black is waiting
    at the same FEN (post its own bestmove). White's ``info`` lines
    appear on black's queue tagged ``paired=True``."""
    # White's first turn: startpos. Plays e2e4 ⇒ waits at fen-after-e4.
    await orch.ingest_proxy_lines("white", [
        "position startpos",
        "bestmove e2e4",
    ])
    # Black's first turn: position startpos moves e2e4. Plays c7c5 ⇒
    # waits at fen-after-e2e4-c7c5.
    await orch.ingest_proxy_lines("black", [
        "position startpos moves e2e4",
        "bestmove c7c5",
    ])
    # White's second turn: gets the post-c5 position. Now white is
    # thinking; black is already waiting at the same FEN.
    await orch.ingest_proxy_lines("white", [
        "position startpos moves e2e4 c7c5",
    ])

    q_black = orch.subscribe_to_proxy("black")
    while not q_black.empty():
        q_black.get_nowait()

    await orch.ingest_proxy_lines("white", [
        "info depth 12 score cp 25 pv g1f3",
    ])
    # Info coalescing flushes onto the queue; await the entry instead
    # of polling for the timer.
    items = [await q_black.get()]
    while not q_black.empty():
        items.append(q_black.get_nowait())

    paired = [m for m in items if m.get("paired")]
    assert len(paired) == 1
    assert paired[0]["proxy_id"] == "white"
    assert paired[0]["thinking_side"] == "white"
    assert "info " in paired[0]["line"]


@pytest.mark.asyncio
async def test_info_does_not_fan_out_when_no_pair(orch):
    """Without an opposite-color proxy registered, ``info`` is only
    delivered to the proxy's own subscribers."""
    await orch.ingest_proxy_lines("white", ["position startpos"])
    q_white = orch.subscribe_to_proxy("white")
    while not q_white.empty():
        q_white.get_nowait()

    await orch.ingest_proxy_lines("white", [
        "info depth 12 score cp 25 pv e2e4",
    ])
    # Wait for the coalesce slot to land on the queue rather than
    # polling for the timer.
    items = [await q_white.get()]
    while not q_white.empty():
        items.append(q_white.get_nowait())
    # Self-fan-out: info delivered, no ``paired`` flag.
    assert any("info " in m["line"] for m in items)
    assert not any(m.get("paired") for m in items)


@pytest.mark.asyncio
async def test_debug_invariants_pass_in_normal_flow(orch, monkeypatch):
    """With debug asserts on, a typical two-proxy handoff sequence
    must not violate any invariant."""
    import sturddle_view.tournament.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_DEBUG_PAIRING", True)

    await orch.ingest_proxy_lines("white", [
        "position startpos",
        "bestmove e2e4",
    ])
    await orch.ingest_proxy_lines("black", [
        "position startpos moves e2e4",
        "bestmove c7c5",
    ])
    await orch.ingest_proxy_lines("white", [
        "position startpos moves e2e4 c7c5",
    ])
    # If we got here with assertions enabled, invariants held.
    assert "white" in orch._pairing_state
    assert "black" in orch._pairing_state


# ---------------------------------------------------------------------------
# End-to-end: two proxies posting to /internal/proxy + WS subscribers
# ---------------------------------------------------------------------------


def _write_fake_fastchess(tmp_path):
    """Write a script that pretends to be the fastchess binary: ignores
    all args and sleeps until killed. Returns the path to invoke."""
    py = tmp_path / "fake_fastchess.py"
    py.write_text(
        "import time\n"
        "while True:\n"
        "    time.sleep(60)\n"
    )
    if sys.platform.startswith("win"):
        wrapper = tmp_path / "fake_fastchess.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        return str(wrapper)
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(py)


@pytest.fixture
def running_server(tmp_path):
    fake_fc = _write_fake_fastchess(tmp_path)
    env = {
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_TOURNAMENT_FASTCHESS_PATH": fake_fc,
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / "engines.json"),
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        c = httpx.Client(base_url=base)
        try:
            t = c.post("/api/tournaments", json={
                "name": "t",
                "engines": [
                    {"id": "id-A", "name": "A", "cmd": "/bin/A"},
                    {"id": "id-B", "name": "B", "cmd": "/bin/B"},
                ],
            }).json()
            c.post(f"/api/tournaments/{t['id']}/start")
            try:
                yield base, c
            finally:
                c.post(f"/api/tournaments/{t['id']}/stop")
        finally:
            c.close()


@pytest.mark.asyncio
async def test_e2e_paired_info_reaches_opposite_subscriber(running_server):
    from websockets.asyncio.client import connect as ws_connect

    base, client = running_server
    secret = client.get("/_test/tournament/proxy_secret").json()["secret"]
    assert secret, "test server must expose a proxy secret"

    # Drive the two proxies to the rendezvous before subscribing, so
    # the WS doesn't have to filter out unrelated snapshot replays.
    client.post("/internal/proxy", json={
        "proxy_id": "white", "secret": secret,
        "lines": ["position startpos", "bestmove e2e4"],
    })
    client.post("/internal/proxy", json={
        "proxy_id": "black", "secret": secret,
        "lines": ["position startpos moves e2e4", "bestmove c7c5"],
    })
    client.post("/internal/proxy", json={
        "proxy_id": "white", "secret": secret,
        "lines": ["position startpos moves e2e4 c7c5"],
    })

    ws_url = base.replace("http://", "ws://") + "/ws/tournament/proxy/black?token="
    import json as _json
    async with ws_connect(ws_url) as ws_b:
        client.post("/internal/proxy", json={
            "proxy_id": "white", "secret": secret,
            "lines": ["info depth 8 score cp 20 pv g1f3"],
        })
        seen_paired = None
        for _ in range(20):
            msg = _json.loads(await ws_b.recv())
            if msg.get("paired") and msg.get("proxy_id") == "white":
                seen_paired = msg
                break
        assert seen_paired is not None
        assert seen_paired["thinking_side"] == "white"
        assert "info " in seen_paired["line"]
