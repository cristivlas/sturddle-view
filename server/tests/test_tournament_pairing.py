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
import sys

import chess
import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.tournament import fastchess as fc_mod
from sturddle_view.tournament.fastchess import FastchessRunner
from sturddle_view.tournament.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
)
from sturddle_view.tournament.runner import RunSpec
from sturddle_view.tournament.store import TournamentStore


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
    # Info coalescing holds the line for _INFO_COALESCE_MS; let it flush.
    await asyncio.sleep(0.15)

    items = []
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
    await asyncio.sleep(0.15)

    items = []
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


async def _confirm_pair(orch, proxy_w: str, proxy_b: str) -> str:
    """Drive both proxies through one move so the pair is confirmed.
    Returns the pair_id."""
    await orch.proxy_session_started(proxy_w, "EngineA")
    await orch.proxy_session_started(proxy_b, "EngineB")
    await orch.ingest_proxy_lines(proxy_w, ["position startpos", "bestmove e2e4"])
    await orch.ingest_proxy_lines(proxy_b, ["position startpos moves e2e4"])
    pair_id = orch._pair_ids.get(frozenset((proxy_w, proxy_b)))
    assert pair_id, "pair not confirmed"
    return pair_id


def _drain(q) -> list[dict]:
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


@pytest.mark.asyncio
async def test_game_subscriber_canonical_order_under_race(orch):
    """Race: white's ``position`` (ply 2) arrives before black's
    ``bestmove`` (ply 1). Subscriber must see canonical ply order:
    black bestmove(1) -> white position(2) -> white bestmove(2)."""
    pair_id = await _confirm_pair(orch, "white", "black")
    q = orch.subscribe_to_game(pair_id)
    _drain(q)  # discard snapshot replay

    # Race: white's events for ply=2 arrive before black's bestmove(ply=1).
    await orch.ingest_proxy_lines("white", [
        "position startpos moves e2e4 c7c5",
        "bestmove g1f3",
    ])
    await orch.ingest_proxy_lines("black", ["bestmove c7c5"])

    items = _drain(q)
    seq = [
        (m["parsed"]["kind"], m["proxy_id"])
        for m in items if m.get("parsed")
    ]
    assert seq == [
        ("bestmove", "black"),
        ("position", "white"),
        ("bestmove", "white"),
    ]


@pytest.mark.asyncio
async def test_game_subscriber_normal_order_unaffected(orch):
    """When events arrive in canonical order (black bestmove then
    white position) the subscriber sees them in that same order."""
    pair_id = await _confirm_pair(orch, "white", "black")
    q = orch.subscribe_to_game(pair_id)
    _drain(q)

    await orch.ingest_proxy_lines("black", ["bestmove c7c5"])
    await orch.ingest_proxy_lines("white", [
        "position startpos moves e2e4 c7c5",
        "bestmove g1f3",
    ])

    items = _drain(q)
    seq = [
        (m["parsed"]["kind"], m["proxy_id"])
        for m in items if m.get("parsed")
    ]
    assert seq == [
        ("bestmove", "black"),
        ("position", "white"),
        ("bestmove", "white"),
    ]


# ---------------------------------------------------------------------------
# End-to-end: two proxies posting to /internal/proxy + WS subscribers
# ---------------------------------------------------------------------------


FAKE_FASTCHESS = r"""
import sys, time
i = 1
while i < len(sys.argv):
    a = sys.argv[i]
    if a == "--sleep":
        time.sleep(float(sys.argv[i+1])); i += 2
    else:
        i += 1
"""


@pytest.fixture
def running_app(tmp_path, monkeypatch):
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
        t = c.post("/api/tournaments", json={
            "name": "t",
            "engines": [{"id": "id-A", "name": "A", "cmd": "/bin/A"}, {"id": "id-B", "name": "B", "cmd": "/bin/B"}],
        }).json()
        c.post(f"/api/tournaments/{t['id']}/start")
        yield c, app
        c.post(f"/api/tournaments/{t['id']}/stop")


def test_e2e_paired_info_reaches_opposite_subscriber(running_app):
    client, app = running_app
    secret = app.state.tournament_orch.proxy_secret()

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

    with client.websocket_connect("/ws/tournament/proxy/black?token=") as ws_b:
        client.post("/internal/proxy", json={
            "proxy_id": "white", "secret": secret,
            "lines": ["info depth 8 score cp 20 pv g1f3"],
        })
        seen_paired = None
        for _ in range(10):
            msg = ws_b.receive_json(mode="text")
            if msg.get("paired") and msg.get("proxy_id") == "white":
                seen_paired = msg
                break
        assert seen_paired is not None
        assert seen_paired["thinking_side"] == "white"
        assert "info " in seen_paired["line"]
