"""PGN reconciliation: orchestrator integration.

Covers move-list capture, the match queue, and `game_reconciled`
emission. Pure-queue mechanics live in `test_pgn_reconcile_queue.py`;
the PGN tailer lives in `test_pgn_tail.py`.
"""
from __future__ import annotations


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
# match queue + `game_reconciled` emission
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
    """A record that whole-game-matches no confirmed pair (capture is
    still too far behind) sits in the buffer until dissolution."""
    full = _LONG_MOVES + ["c2c3", "e8g8", "h2h3"]
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)  # 12 of 15 plies

    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1/2-1/2", termination="adjudication",
        uci_moves=full, game_n=3, round_tag="1",
    )
    await orch._on_pgn_record(record)
    assert _events_of(emitted, "game_reconciled") == []
    assert orch._pair_ids  # pair not dissolved by the too-long record

    # Capture catches up, then the pair dissolves normally.
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves " + " ".join(full),
    ])
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
# Tailer lifecycle is tournament-scoped: subscribers don't start/stop it.
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
async def test_subscribe_does_not_touch_tailer_lifecycle(orch, tmp_path):
    """The tailer runs tournament-scoped (started in start(), stopped
    at teardown); watchers coming and going must not affect it."""
    tailer = _install_tailer(orch, tmp_path)
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)

    q = orch.subscribe_to_game(pair_id)
    assert not tailer.is_running()

    await tailer.start()
    orch.unsubscribe_from_game(pair_id, q)
    assert tailer.is_running()
    await tailer.stop()


@pytest.mark.asyncio
async def test_dissolution_and_drain_leave_tailer_running(orch, tmp_path):
    """Neither a dissolve (pending queued) nor the matching record
    (queue drained, no watchers) stops the tournament-scoped tailer."""
    tailer = _install_tailer(orch, tmp_path)
    await _drive_pair_to(orch, _LONG_MOVES)
    await tailer.start()

    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])
    assert orch._reconcile_queue.pending_count == 1
    assert tailer.is_running()

    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="normal",
        uci_moves=list(_LONG_MOVES), game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(record)
    assert orch._reconcile_queue.pending_count == 0
    assert tailer.is_running()
    await tailer.stop()


# ---------------------------------------------------------------------------
# Dissolve-on-PGN-record: idle engines send no ucinewgame; the record
# itself is the game-end signal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pgn_record_dissolves_idle_confirmed_pair(orch, emitted):
    """No ucinewgame anywhere: the record alone must dissolve the pair
    with the real result and reconcile in the same pass."""
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)

    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="normal",
        uci_moves=list(_LONG_MOVES), game_n=4, round_tag="1",
    )
    await orch._on_pgn_record(record)

    finished = _events_of(emitted, "game_finished")
    assert len(finished) == 1
    assert finished[0]["pair_id"] == pair_id
    assert finished[0]["result"] == "1-0"
    assert finished[0]["termination"] == "normal"
    reconciled = _events_of(emitted, "game_reconciled")
    assert len(reconciled) == 1
    assert reconciled[0]["pair_id"] == pair_id
    assert reconciled[0]["game_n"] == 4
    assert pair_id not in orch._pair_moves
    assert not orch._pair_ids


@pytest.mark.asyncio
async def test_pgn_record_tolerates_captured_shortfall(orch, emitted):
    """Captured normally trails the PGN by a ply or two (no position
    after the final bestmove); the pair still dissolves."""
    full = _LONG_MOVES + ["c2c3", "e8g8"]
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)  # 12 of 14 plies

    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="0-1", termination="normal",
        uci_moves=full, game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(record)

    finished = _events_of(emitted, "game_finished")
    assert len(finished) == 1
    assert finished[0]["pair_id"] == pair_id
    assert finished[0]["result"] == "0-1"


@pytest.mark.asyncio
async def test_pgn_record_prefix_does_not_dissolve_in_progress_pair(orch, emitted):
    """A rematch replaying the same line: the in-progress pair's
    captured moves are a too-short prefix of the finished record.
    No dissolve; the record parks for normal reconciliation."""
    full = _LONG_MOVES + ["c2c3", "e8g8", "h2h3"]
    await _drive_pair_to(orch, _LONG_MOVES)  # 12 of 15 plies: shortfall 3

    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="normal",
        uci_moves=full, game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(record)

    assert _events_of(emitted, "game_finished") == []
    assert orch._pair_ids
    assert orch._reconcile_queue.pgn_buffer_count == 1


@pytest.mark.asyncio
async def test_pgn_record_swapped_colors_do_not_dissolve(orch, emitted):
    """Colors-swapped rematch on the same opening: game 1's late record
    must not dissolve game 2's pair -- names must match by color."""
    await _drive_pair_to(orch, _LONG_MOVES)  # Engine A is white

    record = PgnGameRecord(
        white="Engine B", black="Engine A",
        result="1-0", termination="normal",
        uci_moves=list(_LONG_MOVES), game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(record)

    assert _events_of(emitted, "game_finished") == []
    assert orch._pair_ids


@pytest.mark.asyncio
async def test_pgn_record_divergent_moves_do_not_dissolve(orch, emitted):
    other = list(_LONG_MOVES)
    other[-1] = "h7h6"
    await _drive_pair_to(orch, _LONG_MOVES)

    record = PgnGameRecord(
        white="Engine A", black="Engine B",
        result="1-0", termination="normal",
        uci_moves=other, game_n=1, round_tag="1",
    )
    await orch._on_pgn_record(record)

    assert _events_of(emitted, "game_finished") == []
    assert orch._pair_ids


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
    fastchess having flushed before exit). Tailer installed but not
    started -- teardown must finalize() it anyway.
    """
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    pgn_path = tmp_path / "games.pgn"
    _write_pgn_for_long_moves(pgn_path)
    orch._pgn_tailer = PgnTailer(pgn_path, orch._on_pgn_record, poll_interval=0.01)
    assert not orch._pgn_tailer.is_running()

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
    """Same teardown path with the tailer running (the normal case now).
    finalize() must still drive it to EOF and exit cleanly."""
    pair_id = await _drive_pair_to(orch, _LONG_MOVES)
    pgn_path = tmp_path / "games.pgn"
    pgn_path.touch()
    orch._pgn_tailer = PgnTailer(pgn_path, orch._on_pgn_record, poll_interval=0.05)
    await orch._pgn_tailer.start()
    assert orch._pgn_tailer.is_running()

    # Now the PGN flush lands (fastchess writes the game).
    _write_pgn_for_long_moves(pgn_path)

    await orch._on_runner_event("done", {"rc": 0})

    reconciled = _events_of(emitted, "game_reconciled")
    assert len(reconciled) == 1
    assert reconciled[0]["pair_id"] == pair_id
    assert orch._pgn_tailer is None


