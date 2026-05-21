"""PGN reconciliation: orchestrator integration.

Slice 1 covers move-list capture; slice 3 covers the match queue +
`game_reconciled` emission. Pure-queue mechanics live in
`test_pgn_reconcile_queue.py`; the PGN tailer lives in
`test_pgn_tail.py`.

See `docs/pgn-reconciliation.md`.
"""
from __future__ import annotations

import asyncio

import pytest

from sturddle_view.tournament.orchestrator import Orchestrator
from sturddle_view.tournament.pgn_reconcile import MIN_PLIES_FOR_MATCH
from sturddle_view.tournament.pgn_tail import PgnGameRecord, PgnTailer
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


# ---------------------------------------------------------------------------
# PGN tailer is gated on game subscribers: no watchers => no 1Hz poll.
# ---------------------------------------------------------------------------


def _install_tailer(orch, tmp_path) -> PgnTailer:
    """Attach a real PgnTailer to the orchestrator without going
    through start(). Returns the tailer so tests can inspect it."""
    pgn_path = tmp_path / "games.pgn"
    pgn_path.touch()
    tailer = PgnTailer(pgn_path, orch._on_pgn_record, poll_interval=0.01)
    orch._pgn_tailer = tailer
    return tailer




@pytest.mark.asyncio
async def test_tailer_does_not_start_without_subscribers(orch, tmp_path):
    """No watcher => tailer stays idle. The orchestrator constructs
    the tailer in start(), but the poll loop is gated."""
    tailer = _install_tailer(orch, tmp_path)
    await _drive_pair_to(orch, _LONG_MOVES)
    assert not tailer.is_running()


@pytest.mark.asyncio
async def test_tailer_starts_on_first_subscriber(orch, tmp_path):
    """0->1 transition starts the poll loop."""
    tailer = _install_tailer(orch, tmp_path)
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    assert not tailer.is_running()

    q = orch.subscribe_to_game(pair_id)
    try:
        await orch._await_pending_tailer_tasks()
        assert tailer.is_running()
    finally:
        orch.unsubscribe_from_game(pair_id, q)
        await orch._await_pending_tailer_tasks()
        assert not tailer.is_running()


@pytest.mark.asyncio
async def test_tailer_stops_on_last_unsubscribe(orch, tmp_path):
    """1->0 transition via unsubscribe stops the poll loop."""
    tailer = _install_tailer(orch, tmp_path)
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    q = orch.subscribe_to_game(pair_id)
    await orch._await_pending_tailer_tasks()
    assert tailer.is_running()

    orch.unsubscribe_from_game(pair_id, q)
    await orch._await_pending_tailer_tasks()
    assert not tailer.is_running()


@pytest.mark.asyncio
async def test_tailer_keeps_running_while_pending_entry_unmatched(orch, tmp_path):
    """Dissolution while watching queues a PendingMatch. The tailer
    must keep running so the PGN flush has a chance to reconcile;
    stopping immediately would silently drop the result."""
    tailer = _install_tailer(orch, tmp_path)
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    q = orch.subscribe_to_game(pair_id)
    await orch._await_pending_tailer_tasks()
    assert tailer.is_running()

    # Dissolve. Subscriber dict empties, but pending_count > 0.
    # Gate must skip the stop.
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])
    assert orch._reconcile_queue.pending_count == 1
    assert not orch._game_subscribers
    # Any scheduled stop task must run and find pending entries -> no-op.
    await orch._await_pending_tailer_tasks()
    assert tailer.is_running()

    # Delivering the matching PGN record drains the queue and the
    # post-match drain check stops the tailer.
    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="normal",
        uci_moves=list(_LONG_MOVES), game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(record)
    await orch._await_pending_tailer_tasks()
    assert not tailer.is_running()
    orch.unsubscribe_from_game(pair_id, q)  # cleanup


@pytest.mark.asyncio
async def test_tailer_stays_up_until_all_pending_drain(orch, tmp_path, emitted):
    """Multi-pair regression: with N pending entries queued and no
    subscribers, the tailer must keep running until the last entry
    matches. Stopping early silently drops reconciliations for the
    unmatched pairs -- the bug seen with 4 watched games where the
    last one to dissolve missed its result.

    Uses one real confirmed pair (to exercise the full subscribe ->
    dissolve gate) plus a synthetic PendingMatch injected directly
    into the queue, since the test orchestrator can only confirm one
    FEN-bucketed pair at a time.
    """
    from sturddle_view.tournament.pgn_reconcile import PendingMatch

    tailer = _install_tailer(orch, tmp_path)
    real_pair_moves = list(_LONG_MOVES)
    synth_moves = list(_LONG_MOVES)
    synth_moves[-1] = "c8e6"  # diverge so PGN matcher can tell them apart

    # One confirmed pair via the real subscribe path.
    pair_id = await _drive_pair_to(orch, real_pair_moves)
    q = orch.subscribe_to_game(pair_id)
    await orch._await_pending_tailer_tasks()
    assert tailer.is_running()

    # Inject a second pending entry as if a second watched pair had
    # just dissolved.
    synth_entry = PendingMatch(
        pair_id="synthetic-pair",
        white_proxy="synth-w", black_proxy="synth-b",
        white_engine="Synth W", black_engine="Synth B",
        uci_moves=list(synth_moves),
    )
    orch._reconcile_queue.add_pending(synth_entry)

    # Dissolve the real pair. After this, _game_subscribers is empty
    # but pending_count == 2 (real + synthetic).
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])
    assert not orch._game_subscribers
    assert orch._reconcile_queue.pending_count == 2

    await orch._await_pending_tailer_tasks()
    assert tailer.is_running(), "tailer must remain up while pending entries are queued"

    # First PGN record matches the real pair, drains one entry; the
    # tailer must keep running because pending_count is still 1.
    rec_real = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="normal",
        uci_moves=list(real_pair_moves), game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(rec_real)
    assert orch._reconcile_queue.pending_count == 1
    await orch._await_pending_tailer_tasks()
    assert tailer.is_running(), "tailer must remain up until the last pending entry drains"

    # Second record matches the synthetic entry; queue empty, stop.
    rec_synth = PgnGameRecord(
        white="Synth W", black="Synth B",
        result="0-1", termination="normal",
        uci_moves=list(synth_moves), game_n=2, round_tag="1",
    )
    await orch._on_pgn_record(rec_synth)
    assert orch._reconcile_queue.pending_count == 0
    await orch._await_pending_tailer_tasks()
    assert not tailer.is_running()

    reconciled = _events_of(emitted, "game_reconciled")
    assert len(reconciled) == 2
    reconciled_pair_ids = {r["pair_id"] for r in reconciled}
    assert reconciled_pair_ids == {pair_id, "synthetic-pair"}

    orch.unsubscribe_from_game(pair_id, q)


@pytest.mark.asyncio
async def test_subscribe_during_pending_stop_keeps_tailer_alive(orch, tmp_path):
    """Race: an unsubscribe schedules `tailer.stop()` as a create_task;
    before the task runs, a new subscribe arrives. Without the
    re-check helper the subscribe would see `is_running()` True
    (stop hasn't executed yet) and skip starting -- then the pending
    stop would shut the tailer down, leaving the new subscriber with
    no reconciliation pipeline. The helper re-evaluates at task run
    time so the stop becomes a no-op.
    """
    tailer = _install_tailer(orch, tmp_path)
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    q1 = orch.subscribe_to_game(pair_id)
    await orch._await_pending_tailer_tasks()
    assert tailer.is_running()

    # Tight race: unsubscribe schedules stop; subscribe scheduled
    # immediately, before any yield. Both stop and start tasks now
    # sit in the ready queue.
    orch.unsubscribe_from_game(pair_id, q1)
    q2 = orch.subscribe_to_game(pair_id)

    # Await BOTH scheduled tasks (stop + start). The re-check inside
    # _maybe_stop_tailer must make the stop a no-op.
    await orch._await_pending_tailer_tasks()

    assert tailer.is_running(), (
        "tailer must remain up: a new subscriber arrived before the "
        "pending stop ran, so the stop should have re-checked and skipped"
    )
    orch.unsubscribe_from_game(pair_id, q2)
    await orch._await_pending_tailer_tasks()
    assert not tailer.is_running()


@pytest.mark.asyncio
async def test_records_flow_only_while_subscribed(orch, tmp_path, emitted):
    """End-to-end: PGN record routed through _on_pgn_record while
    subscribed emits game_reconciled. After unsubscribe, the tailer
    is stopped; orchestrator-level emission still works when the
    record is delivered directly (callers gate, not the callback)."""
    _install_tailer(orch, tmp_path)
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    q = orch.subscribe_to_game(pair_id)
    await orch._await_pending_tailer_tasks()
    assert orch._pgn_tailer.is_running()
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="normal",
        uci_moves=list(_LONG_MOVES), game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(record)
    assert len(_events_of(emitted, "game_reconciled")) == 1
    orch.unsubscribe_from_game(pair_id, q)
    await orch._await_pending_tailer_tasks()
    assert not orch._pgn_tailer.is_running()


# ---------------------------------------------------------------------------
# Terminal teardown: dissolve parks entries; finalize drains PGN; matches.
# ---------------------------------------------------------------------------


_PGN_GAME_TEMPLATE = """\
[Event "Test"]
[Site "?"]
[Round "1"]
[White "{white}"]
[Black "{black}"]
[Result "{result}"]
[Termination "{termination}"]

{movetext} {result}

"""


def _write_pgn_for_long_moves(pgn_path, white="Engine A", black="Engine B",
                              result="1-0", termination="normal") -> None:
    """Write a PGN file containing one game whose movetext matches
    _LONG_MOVES (Ruy Lopez Berlin, 12 plies)."""
    # SAN equivalent of _LONG_MOVES, hand-derived.
    movetext = (
        "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Bxa6 Nf6 "
        "5. O-O Be7 6. Re1 d6"
    )
    pgn_path.write_text(
        _PGN_GAME_TEMPLATE.format(
            white=white, black=black, result=result,
            termination=termination, movetext=movetext,
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_terminal_teardown_reconciles_inflight_game(orch, tmp_path, emitted):
    """The bug this commit fixes: at tournament end (runner emits
    `done`), a pair that hadn't dissolved via ucinewgame must still
    reconcile against the PGN that fastchess flushed before exit.

    Drives the runner-event handler directly. Setup: confirmed pair,
    PGN file with a matching game already on disk (simulating
    fastchess having flushed before exit). No subscriber, so the
    tailer is paused -- teardown must finalize() it anyway.
    """
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    pgn_path = tmp_path / "games.pgn"
    _write_pgn_for_long_moves(pgn_path)
    orch._pgn_tailer = PgnTailer(pgn_path, orch._on_pgn_record, poll_interval=0.01)
    assert not orch._pgn_tailer.is_running()  # no subscribers

    # Drive the runner-event handler with the terminal `done` event.
    # Store update will log an error (no real tournament), but the
    # finally block executes the teardown sequence unchanged.
    await orch._on_runner_event("done", {"rc": 0})

    # The pair must have been dissolved and reconciled against the
    # PGN drained by tailer.finalize().
    reconciled = _events_of(emitted, "game_reconciled")
    assert len(reconciled) == 1
    assert reconciled[0]["pair_id"] == pair_id
    assert reconciled[0]["result"] == "1-0"
    assert reconciled[0]["termination"] == "normal"
    # Tailer was finalized and dropped.
    assert orch._pgn_tailer is None


@pytest.mark.asyncio
async def test_terminal_teardown_with_running_tailer(orch, tmp_path, emitted):
    """Same teardown path, but with a watcher attached so the tailer
    was already running. finalize() must still drive it to EOF and
    exit cleanly."""
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    pgn_path = tmp_path / "games.pgn"
    pgn_path.touch()
    orch._pgn_tailer = PgnTailer(pgn_path, orch._on_pgn_record, poll_interval=0.05)

    q = orch.subscribe_to_game(pair_id)
    await orch._await_pending_tailer_tasks()
    assert orch._pgn_tailer.is_running()

    # Now the PGN flush lands (fastchess writes the game).
    _write_pgn_for_long_moves(pgn_path)

    await orch._on_runner_event("done", {"rc": 0})

    reconciled = _events_of(emitted, "game_reconciled")
    assert len(reconciled) == 1
    assert reconciled[0]["pair_id"] == pair_id
    assert orch._pgn_tailer is None
    orch.unsubscribe_from_game(pair_id, q)


# ---------------------------------------------------------------------------
# _maybe_stop_tailer: each guard condition independently keeps tailer alive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_stop_tailer_keeps_alive_when_subscriber_present(orch, tmp_path):
    """game_subscribers non-empty → tailer must not stop. Kills L1042
    `not _game_subscribers` AddNot/Delete-Not mutations."""
    tailer = _install_tailer(orch, tmp_path)
    await tailer.start()
    assert tailer.is_running()

    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    q = orch.subscribe_to_game(pair_id)

    await orch._maybe_stop_tailer("test")
    assert tailer.is_running()
    orch.unsubscribe_from_game(pair_id, q)
    await tailer.stop()


@pytest.mark.asyncio
async def test_maybe_stop_tailer_keeps_alive_when_pending_reconcile(orch, tmp_path):
    """reconcile_queue.pending_count > 0 → tailer must not stop. Kills
    L1043 `== 0`→`!= 0` / `Gt`/`GtE` comparison mutations."""
    from sturddle_view.tournament.pgn_reconcile import PendingMatch

    tailer = _install_tailer(orch, tmp_path)
    await tailer.start()
    assert tailer.is_running()

    # Park a pending entry so pending_count == 1.
    moves = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6",
             "b5a6", "g8f6", "e1g1", "f8e7", "f1e1", "d7d6"]
    entry = PendingMatch(
        pair_id="p1", white_proxy="w", black_proxy="b",
        white_engine="A", black_engine="B", uci_moves=moves,
    )
    orch._reconcile_queue.add_pending(entry)
    assert orch._reconcile_queue.pending_count == 1

    await orch._maybe_stop_tailer("test")
    assert tailer.is_running()
    await tailer.stop()


@pytest.mark.asyncio
async def test_maybe_stop_tailer_noop_when_tailer_is_none(orch):
    """_pgn_tailer is None → function is a no-op (no crash). Kills L1044
    `is not None`→`is None` mutation."""
    assert orch._pgn_tailer is None
    await orch._maybe_stop_tailer("test")  # must not raise


@pytest.mark.asyncio
async def test_maybe_stop_tailer_noop_when_tailer_not_running(orch, tmp_path):
    """tailer exists but not running → no stop call. Kills L1045
    `is_running()`→`not is_running()` / `ReplaceAndWithOr` mutations."""
    tailer = _install_tailer(orch, tmp_path)
    assert not tailer.is_running()

    await orch._maybe_stop_tailer("test")
    assert not tailer.is_running()  # still not running (was never started)
