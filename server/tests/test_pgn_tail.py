"""PGN tailer (slice 2 of pgn-reconciliation).

Covers delta parsing, offset bookkeeping, in-flight handling, the
fast-skip path when nothing changed, and truncation recovery. The
orchestrator wiring is exercised in `test_pgn_reconciliation.py`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sturddle_view.tournament.pgn_tail import PgnGameRecord, PgnTailer


_ONE_GAME = """\
[Event "My Tournament"]
[Site "?"]
[Round "1"]
[White "Engine A"]
[Black "Engine B"]
[Result "1-0"]
[Termination "normal"]

1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Bxa6 1-0

"""

_SECOND_GAME = """\
[Event "My Tournament"]
[Site "?"]
[Round "2"]
[White "Engine B"]
[Black "Engine A"]
[Result "0-1"]
[Termination "time forfeit"]

1. d4 d5 2. Nf3 0-1

"""

_PARTIAL_GAME = """\
[Event "My Tournament"]
[Site "?"]
[Round "3"]
[White "Engine A"]
[Black "Engine B"]
[Result "*"]

1. e4 e5
"""


def _records_collector():
    out: list[PgnGameRecord] = []
    async def cb(rec: PgnGameRecord) -> None:
        out.append(rec)
    return out, cb


@pytest.fixture
def pgn_path(tmp_path) -> Path:
    return tmp_path / "games.pgn"


# ---------------------------------------------------------------------------
# Parse correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file_is_noop(pgn_path):
    records, cb = _records_collector()
    tailer = PgnTailer(pgn_path, cb)
    n = await tailer.poll_once()
    assert n == 0
    assert records == []
    assert tailer.offset == 0
    assert tailer.game_n == 0


@pytest.mark.asyncio
async def test_single_complete_game(pgn_path):
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n = await tailer.poll_once()

    assert n == 1
    assert len(records) == 1
    rec = records[0]
    assert rec.game_n == 1
    assert rec.white == "Engine A"
    assert rec.black == "Engine B"
    assert rec.result == "1-0"
    assert rec.termination == "normal"
    assert rec.round_tag == "1"
    assert rec.uci_moves == ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a6"]
    # Offset should advance to the end of the file.
    assert tailer.offset == pgn_path.stat().st_size


@pytest.mark.asyncio
async def test_partial_game_held_for_next_pass(pgn_path):
    """A `Result "*"` game is fastchess in flight: leave the offset at
    the last complete game so the in-flight bytes get re-read once the
    final tag and result land."""
    # Write _ONE_GAME first to capture its on-disk byte size (Windows
    # may translate \n -> \r\n), then extend with the partial.
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    one_game_size = pgn_path.stat().st_size
    pgn_path.write_text(_ONE_GAME + _PARTIAL_GAME, encoding="utf-8")

    records, cb = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n = await tailer.poll_once()

    assert n == 1  # only the complete one
    assert tailer.offset == one_game_size
    assert tailer.offset < pgn_path.stat().st_size


@pytest.mark.asyncio
async def test_two_games_appended_over_two_polls(pgn_path):
    """game_n is cumulative across polls, not reset per delta."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n1 = await tailer.poll_once()
    assert n1 == 1
    assert records[0].game_n == 1

    # Append the second game; bump mtime to defeat the no-change guard.
    with pgn_path.open("a", encoding="utf-8") as f:
        f.write(_SECOND_GAME)
    _bump_mtime(pgn_path)

    n2 = await tailer.poll_once()
    assert n2 == 1
    assert len(records) == 2
    assert records[1].game_n == 2
    assert records[1].result == "0-1"
    assert records[1].white == "Engine B"


@pytest.mark.asyncio
async def test_no_change_is_skipped(pgn_path):
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n1 = await tailer.poll_once()
    assert n1 == 1

    # Same mtime + same size + same offset → fast path.
    n2 = await tailer.poll_once()
    assert n2 == 0
    assert len(records) == 1


@pytest.mark.asyncio
async def test_truncation_resets_offset(pgn_path):
    """Defensive: if the file shrinks, reset and reparse from 0. Covers
    test fixtures, manual edits, and a future runner that rotates."""
    pgn_path.write_text(_ONE_GAME + _SECOND_GAME, encoding="utf-8")
    records, cb = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n1 = await tailer.poll_once()
    assert n1 == 2
    assert tailer.game_n == 2

    # Replace with shorter content (single game). Cumulative game_n
    # resets so the new state isn't tagged with stale numbering.
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    _bump_mtime(pgn_path)

    n2 = await tailer.poll_once()
    assert n2 == 1
    # game_n reset to 0 + 1 new = 1, even though we've now emitted 3
    # records total across the two passes.
    assert records[-1].game_n == 1
    assert tailer.game_n == 1


@pytest.mark.asyncio
async def test_resume_pre_existing_pgn_parses_on_first_poll(pgn_path):
    """A tournament Resume starts the tailer against an existing PGN.
    The first poll must parse the whole file — not wait for an append."""
    pgn_path.write_text(_ONE_GAME + _SECOND_GAME, encoding="utf-8")
    records, cb = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n = await tailer.poll_once()
    assert n == 2
    assert [r.game_n for r in records] == [1, 2]


# ---------------------------------------------------------------------------
# Lifecycle (start / stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop(pgn_path):
    """`start` spawns the task; `stop` cancels and joins. Idempotent on
    both ends."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb = _records_collector()
    tailer = PgnTailer(pgn_path, cb, poll_interval=0.05)

    await tailer.start()
    assert tailer.is_running()
    # Idempotent: a second start is a no-op.
    await tailer.start()
    assert tailer.is_running()

    # Give the immediate-pass parse a moment to land.
    await asyncio.sleep(0.1)

    await tailer.stop()
    assert not tailer.is_running()
    # Stop is idempotent.
    await tailer.stop()
    assert len(records) >= 1


@pytest.mark.asyncio
async def test_callback_exceptions_dont_kill_tailer(pgn_path):
    """One bad callback must not stop the loop — log + carry on. Slice
    3 will plug a real consumer in here, and we want any bug there to
    surface as a log line, not a frozen tournament."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")

    calls = 0
    async def bad_cb(rec: PgnGameRecord) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    tailer = PgnTailer(pgn_path, bad_cb)
    n = await tailer.poll_once()
    # Record was attempted; offset still advanced so we don't re-emit.
    assert n == 0  # poll_once counts successful emissions
    assert calls == 1
    assert tailer.offset == pgn_path.stat().st_size


def _bump_mtime(path: Path) -> None:
    """Force a different mtime_ns so the no-change fast-path doesn't
    swallow our test mutation. Filesystems on some hosts have coarse
    mtime granularity (NTFS, HFS+); writing back-to-back in a test
    can land in the same nanosecond bucket."""
    import os
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns + 10_000_000, st.st_mtime_ns + 10_000_000))
