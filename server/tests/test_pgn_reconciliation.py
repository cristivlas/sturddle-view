"""PGN reconciliation: orchestrator integration.

Slice 1 covers move-list capture; slice 3 covers the match queue +
`game_reconciled` emission. Pure-queue mechanics live in
`test_pgn_reconcile_queue.py`; the PGN tailer lives in
`test_pgn_tail.py`.

See `docs/pgn-reconciliation.md`.
"""
from __future__ import annotations

import pytest

from sturddle_view.tournament.orchestrator import Orchestrator
from sturddle_view.tournament.pgn_reconcile import MIN_PLIES_FOR_MATCH
from sturddle_view.tournament.pgn_tail import PgnGameRecord
from sturddle_view.tournament.runner import RunSpec
from sturddle_view.tournament.store import TournamentStore


_ENGINE_A = "Engine A"
_ENGINE_B = "Engine B"
_PROXY_A  = "proxy-a"
_PROXY_B  = "proxy-b"


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


async def _confirm_pair(orch, pid_white: str, pid_black: str) -> None:
    """Same minimal rendezvous used in test_tournament_game_lifecycle."""
    await orch.ingest_proxy_lines(pid_white, [
        "position startpos",
        "bestmove e2e4",
    ])
    await orch.ingest_proxy_lines(pid_black, [
        "position startpos moves e2e4",
    ])


def _pair_id(orch) -> str:
    assert len(orch._pair_ids) == 1
    return next(iter(orch._pair_ids.values()))


@pytest.mark.asyncio
async def test_pair_moves_initialized_on_confirmation(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    pair_id = _pair_id(orch)
    # Confirmation rendezvous already passed one ply through update.
    # The list exists; content is "what we've seen so far" (= ["e2e4"]).
    assert pair_id in orch._pair_moves
    assert orch._pair_moves[pair_id] == ["e2e4"]


@pytest.mark.asyncio
async def test_pair_moves_grows_with_position_lines(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = _pair_id(orch)

    # White receives position with the new ply, then plays its move.
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves e2e4 e7e5",
        "bestmove g1f3",
    ])
    await orch.ingest_proxy_lines(_PROXY_B, [
        "position startpos moves e2e4 e7e5 g1f3",
    ])

    assert orch._pair_moves[pair_id] == ["e2e4", "e7e5", "g1f3"]


@pytest.mark.asyncio
async def test_shorter_or_stale_position_does_not_clobber(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = _pair_id(orch)

    # Drive the list forward to 3 plies.
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves e2e4 e7e5",
    ])
    await orch.ingest_proxy_lines(_PROXY_B, [
        "position startpos moves e2e4 e7e5 g1f3",
    ])
    assert orch._pair_moves[pair_id] == ["e2e4", "e7e5", "g1f3"]

    # A late-arriving shorter frame from White (already covered by
    # what Black reported) must not shrink the list.
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves e2e4 e7e5",
    ])
    assert orch._pair_moves[pair_id] == ["e2e4", "e7e5", "g1f3"]

    # A divergent (non-prefix) shorter list is also rejected. Synthetic
    # -- a real engine wouldn't send this, but the guard belongs in the
    # update code so book-line collisions early in the game can't poison
    # state once it has diverged.
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves d2d4",
    ])
    assert orch._pair_moves[pair_id] == ["e2e4", "e7e5", "g1f3"]


@pytest.mark.asyncio
async def test_pair_moves_dropped_on_dissolution(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = _pair_id(orch)
    assert pair_id in orch._pair_moves

    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    assert pair_id not in orch._pair_moves


@pytest.mark.asyncio
async def test_pair_moves_dropped_on_proxy_session_end(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = _pair_id(orch)
    assert pair_id in orch._pair_moves

    await orch.proxy_session_ended(_PROXY_A)

    assert pair_id not in orch._pair_moves


@pytest.mark.asyncio
async def test_pair_moves_cleared_by_reset_state(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    assert orch._pair_moves

    orch._reset_pairing_state()

    assert orch._pair_moves == {}


@pytest.mark.asyncio
async def test_position_outside_confirmed_pair_is_ignored(orch):
    """Lines from a proxy with no confirmed peer must not allocate
    state -- `_pair_moves` is keyed by pair_id, not proxy_id."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves e2e4",
    ])
    assert orch._pair_moves == {}


# ---------------------------------------------------------------------------
# Slice 3: match queue + `game_reconciled` emission
# ---------------------------------------------------------------------------


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


# A move list long enough to clear the min-plies floor. Reuses the
# Ruy Lopez Berlin from the earlier shared fixture.
_LONG_MOVES = [
    "e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a6",
    "g8f6", "e1g1", "f8e7", "f1e1", "d7d6",
]
assert len(_LONG_MOVES) >= MIN_PLIES_FOR_MATCH


async def _drive_pair_to(orch, moves: list[str]) -> str:
    """Confirm a pair and ingest enough position lines to populate
    `_pair_moves[pair_id]` with the given move list. Returns the
    `pair_id`."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    # Initial rendezvous (white plays first move, black sees it).
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos",
        "bestmove " + moves[0],
    ])
    await orch.ingest_proxy_lines(_PROXY_B, [
        "position startpos moves " + moves[0],
    ])
    pair_id = _pair_id(orch)
    # Drive in the rest. We alternate which proxy reports each new
    # frame to mirror real ingest, but the `_update_pair_moves` rule
    # only requires monotonic prefix-extension, so any order works.
    if len(moves) > 1:
        running = " ".join(moves)
        await orch.ingest_proxy_lines(_PROXY_A, [
            "position startpos moves " + running,
        ])
    return pair_id


@pytest.mark.asyncio
async def test_pgn_record_arrives_after_dissolution_emits_reconciled(orch, emitted):
    """The dissolution side fires first; the PGN tailer's record
    arrives later and triggers reconciliation."""
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)

    # Dissolve. game_finished fires now with result=*; nothing has
    # matched yet.
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])
    finished = _events_of(emitted, "game_finished")
    assert len(finished) == 1
    assert finished[0]["result"] == "*"
    assert finished[0]["game_n"] is None
    assert _events_of(emitted, "game_reconciled") == []

    # Tailer parses a matching PGN game.
    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="normal",
        uci_moves=list(_LONG_MOVES), game_n=7, round_tag="1",
    )
    await orch._on_pgn_record(record)

    reconciled = _events_of(emitted, "game_reconciled")
    assert len(reconciled) == 1
    r = reconciled[0]
    assert r["pair_id"] == pair_id
    assert r["result"] == "1-0"
    assert r["termination"] == "normal"
    assert r["game_n"] == 7
    assert r["matched"] is True
    assert r["ply_count"] == len(_LONG_MOVES)


@pytest.mark.asyncio
async def test_pgn_record_arrives_before_dissolution(orch, emitted):
    """If the PGN flush wins the race, the record sits in the buffer
    until the dissolution shows up."""
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)

    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1/2-1/2", termination="adjudication",
        uci_moves=list(_LONG_MOVES), game_n=3, round_tag="1",
    )
    await orch._on_pgn_record(record)
    assert _events_of(emitted, "game_reconciled") == []

    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    reconciled = _events_of(emitted, "game_reconciled")
    assert len(reconciled) == 1
    assert reconciled[0]["pair_id"] == pair_id
    assert reconciled[0]["result"] == "1/2-1/2"
    assert reconciled[0]["game_n"] == 3


@pytest.mark.asyncio
async def test_short_game_does_not_attempt_reconciliation(orch, emitted):
    """Below the min-plies floor the dissolution fires `game_finished`
    as today and no reconciliation is attempted, even if a PGN record
    is available."""
    await _drive_pair_to(orch, ["e2e4"])  # 1 ply, way below floor

    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    # Even handing the matching PGN record in does not produce an event.
    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="?",
        uci_moves=["e2e4"], game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(record)
    assert _events_of(emitted, "game_reconciled") == []


@pytest.mark.asyncio
async def test_reset_state_clears_reconcile_queue(orch, emitted):
    """Terminal teardown must drop pending entries so a follow-up
    tournament doesn't see ghost matches from the previous one."""
    await _drive_pair_to(orch, _LONG_MOVES)
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])
    orch._reset_pairing_state()

    # After reset, a matching PGN record produces no event -- the
    # pending entry that would have matched it is gone.
    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="normal",
        uci_moves=list(_LONG_MOVES), game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(record)
    assert _events_of(emitted, "game_reconciled") == []


@pytest.mark.asyncio
async def test_non_matching_pgn_record_does_not_emit(orch, emitted):
    """A PGN record whose move list doesn't match any pending entry
    sits in the buffer; no event is emitted until something matches."""
    await _drive_pair_to(orch, _LONG_MOVES)
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    other = list(_LONG_MOVES)
    other[-1] = "h7h6"  # diverge

    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="?",
        uci_moves=other, game_n=99, round_tag="1",
    )
    await orch._on_pgn_record(record)
    assert _events_of(emitted, "game_reconciled") == []
