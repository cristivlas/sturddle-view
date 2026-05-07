"""Pair confirmation and dissolution.

Pair dissolution is the sole game-end signal: when one of a confirmed
pair's proxies leaves its FEN bucket (typically via ``ucinewgame``), the
pair's game ends and ``game_finished`` fires. Result/termination are
reported as UNKNOWN — fastchess stdout correlation under concurrency is
unreliable. The schema fields are kept for a future implementation that
can populate them via a different signal.

See `docs/tournament-spec.md` § "Pair lifecycle" for the design.
"""
from __future__ import annotations

import pytest

from sturddle_view.tournament.orchestrator import (
    Orchestrator,
    _RESULT_UNKNOWN,
    _TERMINATION_UNKNOWN,
)
from sturddle_view.tournament.runner import RunSpec
from sturddle_view.tournament.store import TournamentStore


_ENGINE_A = "Engine A"
_ENGINE_B = "Engine B"
_PROXY_A  = "proxy-a"
_PROXY_B  = "proxy-b"
_PROXY_C  = "proxy-c"


class _FakeRunner:
    binary_path = "/fake/fastchess"
    def is_running(self) -> bool: return False
    async def start(self, spec: RunSpec, on_event) -> None: pass  # noqa: ARG002
    async def stop(self) -> None: pass


@pytest.fixture
def orch(tmp_path):
    store = TournamentStore(tmp_path / "tournaments")
    o = Orchestrator(store, _FakeRunner())
    o._active_id = "tournament-x"
    return o


@pytest.fixture
def emitted(orch):
    """Capture every event the orchestrator broadcasts."""
    events: list[tuple[str, dict]] = []
    async def cb(kind: str, payload: dict) -> None:
        events.append((kind, payload))
    orch.set_broadcast(cb)
    return events


def _events_of(emitted, kind: str) -> list[dict]:
    return [p for k, p in emitted if k == kind]


async def _confirm_pair(orch, pid_white: str, pid_black: str) -> None:
    """Drive the minimal UCI sequence that confirms a (white, black) pair."""
    await orch.ingest_proxy_lines(pid_white, [
        "position startpos",
        "bestmove e2e4",
    ])
    await orch.ingest_proxy_lines(pid_black, [
        "position startpos moves e2e4",
    ])


# ---------------------------------------------------------------------------
# Pair confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_confirms_on_rendezvous(orch, emitted):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)

    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    paired = _events_of(emitted, "proxy_paired")
    assert len(paired) == 1
    assert paired[0]["pair_id"]  # non-empty UUID
    assert _PROXY_A in orch._confirmed_pairs
    assert _PROXY_B in orch._confirmed_pairs


@pytest.mark.asyncio
async def test_payload_orientation_a_is_white_b_is_black(orch, emitted):
    """`proxy_paired` and `game_finished` must always put white in `_a`
    and black in `_b`. Frozenset-iteration order is nondeterministic;
    if we let it leak through, downstream UI mislabels the board."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    paired = _events_of(emitted, "proxy_paired")[0]
    assert paired["side_a"] == "white"
    assert paired["side_b"] == "black"
    assert paired["proxy_a"] == _PROXY_A and paired["engine_a"] == _ENGINE_A
    assert paired["proxy_b"] == _PROXY_B and paired["engine_b"] == _ENGINE_B

    # Trigger dissolution via ucinewgame.
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])
    finished = _events_of(emitted, "game_finished")[0]
    assert finished["proxy_a"] == _PROXY_A and finished["engine_a"] == _ENGINE_A
    assert finished["proxy_b"] == _PROXY_B and finished["engine_b"] == _ENGINE_B


@pytest.mark.asyncio
async def test_active_pairings_orientation(orch):
    """REST snapshot must also use white-in-_a orientation so a
    late-attaching workspace doesn't disagree with live-event payloads."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    snapshot = orch.active_pairings()
    assert len(snapshot) == 1
    entry = snapshot[0]
    assert entry["side_a"] == "white"
    assert entry["side_b"] == "black"
    assert entry["proxy_a"] == _PROXY_A and entry["engine_a"] == _ENGINE_A
    assert entry["proxy_b"] == _PROXY_B and entry["engine_b"] == _ENGINE_B


@pytest.mark.asyncio
async def test_same_engine_name_pair_rejected_as_phantom(orch, emitted):
    """Book-line collisions can leave two same-engine proxies (one
    white, one black, from different actual slots) sharing a FEN.
    Pair detection must reject them — they aren't playing each other.
    See spec § "Self-play (deferred)" for when this rule lifts."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_A)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    assert _events_of(emitted, "proxy_paired") == []
    assert not orch._pair_proxies
    assert _PROXY_A not in orch._confirmed_pairs


# ---------------------------------------------------------------------------
# Game end via dissolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ucinewgame_dissolves_pair(orch, emitted):
    """A proxy entering its next game (ucinewgame) ends the current
    pair's game and fires `game_finished` with UNKNOWN result."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = next(iter(orch._pair_proxies))

    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    finished = _events_of(emitted, "game_finished")
    assert len(finished) == 1
    payload = finished[0]
    assert payload["pair_id"] == pair_id
    assert payload["tournament_id"] == "tournament-x"
    assert payload["game_n"] is None
    assert payload["result"] == _RESULT_UNKNOWN
    assert payload["termination"] == _TERMINATION_UNKNOWN
    # `proxy_unpaired` is still emitted alongside (debug signal).
    assert len(_events_of(emitted, "proxy_unpaired")) == 1


@pytest.mark.asyncio
async def test_dissolve_drops_pair_state(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = next(iter(orch._pair_proxies))

    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    assert pair_id not in orch._pair_proxies
    assert pair_id not in orch._pair_ids.values()
    assert _PROXY_A not in orch._confirmed_pairs
    assert _PROXY_B not in orch._confirmed_pairs


@pytest.mark.asyncio
async def test_dissolve_sends_ws_sentinel(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = next(iter(orch._pair_proxies))
    q = orch.subscribe_to_game(pair_id)
    while not q.empty():
        q.get_nowait()

    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    msg = q.get_nowait()
    assert msg["ended"] is True
    assert msg["result"] == _RESULT_UNKNOWN
    assert msg["termination"] == _TERMINATION_UNKNOWN


@pytest.mark.asyncio
async def test_proxy_session_ended_dissolves_pair(orch, emitted):
    """A peer disconnecting mid-game also ends the pair via the
    orphaned-from-bucket path."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    await orch.proxy_session_ended(_PROXY_A)

    assert len(_events_of(emitted, "proxy_ended")) == 1
    assert len(_events_of(emitted, "game_finished")) == 1


@pytest.mark.asyncio
async def test_surviving_proxy_can_repair(orch):
    """When a peer dies mid-game, the surviving proxy must be free to
    confirm a fresh pair on its next ucinewgame."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch.proxy_session_started(_PROXY_C, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    # Peer B dies mid-game — pair dissolves.
    await orch.proxy_session_ended(_PROXY_B)
    assert _PROXY_A not in orch._confirmed_pairs

    # A starts a fresh game with C.
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])
    await _confirm_pair(orch, _PROXY_A, _PROXY_C)

    assert orch._confirmed_pairs.get(_PROXY_A) == _PROXY_C
    assert orch._confirmed_pairs.get(_PROXY_C) == _PROXY_A


# ---------------------------------------------------------------------------
# Force-dissolve on terminal runner event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dissolve_all_pairs_drains_open_pairs(orch, emitted):
    """`_dissolve_all_pairs` runs on terminal runner events so any
    open game-WS subscribers receive the `ended` sentinel."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    await orch._dissolve_all_pairs()

    finished = _events_of(emitted, "game_finished")
    assert len(finished) == 1
    assert finished[0]["result"] == _RESULT_UNKNOWN
    assert finished[0]["termination"] == _TERMINATION_UNKNOWN
    assert not orch._pair_proxies
