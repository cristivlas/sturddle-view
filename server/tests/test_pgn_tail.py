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

_ILLEGAL_MOVE_GAME = """\
[Event "My Tournament"]
[Site "?"]
[Round "3"]
[White "Engine A"]
[Black "Engine B"]
[Result "1-0"]
[Termination "normal"]

1. e4 e5 2. Qh8 1-0

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
    """Collector + asyncio.Event signalled on every append.

    Tests waiting for "the next record" do ``await event.wait()`` and
    then ``event.clear()`` before the next batch -- no sleeps."""
    out: list[PgnGameRecord] = []
    received = asyncio.Event()
    async def cb(rec: PgnGameRecord) -> None:
        out.append(rec)
        received.set()
    return out, cb, received


@pytest.fixture
def pgn_path(tmp_path) -> Path:
    return tmp_path / "games.pgn"


def _write_pgn_text(path: Path, text: str) -> None:
    """Write PGN text with explicit `\\n`-only line endings.

    `Path.write_text` on Windows translates `\\n` to `\\r\\n` via the
    default newline handler, which breaks any test that inspects byte
    offsets or scans for the `\\n\\n[` game-boundary separator. Use
    this helper whenever the test cares about on-disk byte layout."""
    path.write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Parse correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_file_is_noop(pgn_path):
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)
    n = await tailer.poll_once()
    assert n == 0
    assert records == []
    assert tailer.offset == 0
    assert tailer.game_n == 0


@pytest.mark.asyncio
async def test_single_complete_game(pgn_path):
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
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

    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n = await tailer.poll_once()

    assert n == 1  # only the complete one
    assert tailer.offset == one_game_size
    assert tailer.offset < pgn_path.stat().st_size


@pytest.mark.asyncio
async def test_two_games_appended_over_two_polls(pgn_path):
    """game_n is cumulative across polls, not reset per delta."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
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
async def test_illegal_move_game_skipped_valid_game_emitted(pgn_path):
    """A game with an illegal SAN is skipped; the following valid game is
    still emitted with the correct game_n and the offset advances past both."""
    pgn_path.write_text(_ILLEGAL_MOVE_GAME + _SECOND_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n = await tailer.poll_once()

    assert n == 1
    assert len(records) == 1
    assert records[0].game_n == 1
    assert records[0].result == "0-1"
    assert tailer.offset == pgn_path.stat().st_size


@pytest.mark.asyncio
async def test_poll_once_has_more_true_when_delta_capped(pgn_path, monkeypatch):
    """When _snap_to_boundary caps the delta below file_size, poll_once
    must set _has_more=True so the run loop polls again without sleeping."""
    from sturddle_view.tournament import pgn_tail as pt_mod
    # Force a tiny cap so two games can't both fit in one poll.
    monkeypatch.setattr(pt_mod, "_MAX_DELTA_BYTES_PER_POLL", 200)

    # write_bytes (not write_text) so Windows doesn't translate \n -> \r\n;
    # the snap-to-boundary `\n\n[` separator must match what's on disk.
    _write_pgn_text(pgn_path, _ONE_GAME + _SECOND_GAME)
    records, cb, _ = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n = await tailer.poll_once()
    assert n == 1                # only the first game fit
    assert tailer._has_more is True   # signals the run loop to keep going
    assert tailer.offset < pgn_path.stat().st_size


@pytest.mark.asyncio
async def test_poll_once_has_more_false_when_delta_fully_consumed(pgn_path):
    """When the whole file is consumed in one poll, _has_more=False so
    the run loop sleeps before polling again."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _ = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n = await tailer.poll_once()
    assert n == 1
    assert tailer._has_more is False
    assert tailer.offset == pgn_path.stat().st_size


@pytest.mark.asyncio
async def test_poll_once_size_equals_offset_clears_has_more(pgn_path):
    """File size == consumed offset but fast-skip didn't trigger (mtime
    bumped without new bytes): must clear _has_more and return 0."""
    _write_pgn_text(pgn_path, _ONE_GAME)
    records, cb, _ = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n1 = await tailer.poll_once()
    assert n1 == 1
    tailer._has_more = True  # pretend a prior caller set it
    # Force the fast-skip predicate to fail by invalidating the cached
    # mtime directly. Bumping mtime via os.utime is unreliable across
    # platforms (NTFS may round nanoseconds; FAT has 2s resolution).
    tailer._last_mtime_ns = 0

    n2 = await tailer.poll_once()
    assert n2 == 0
    assert tailer._has_more is False


def test_snap_to_boundary_returns_exact_offset(pgn_path):
    """When a `\\n\\n[` separator is found in the window, snap returns the
    offset *just after* the separator's blank line (start + sep + 2)."""
    _write_pgn_text(pgn_path, _ONE_GAME + _SECOND_GAME)
    records, cb, _ = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    file_size = pgn_path.stat().st_size
    boundary = tailer._snap_to_boundary(0, file_size, file_size)

    blob = pgn_path.read_bytes()
    # The snapped offset must land at a `[` (start of a tag line),
    # confirming start + sep + 2 hit the right spot.
    assert 0 < boundary < file_size
    assert blob[boundary:boundary + 1] == b"["
    # And it must be the LAST such boundary (rfind), i.e. the start of game 2.
    assert boundary == blob.rfind(b"\n\n[") + 2


def test_snap_to_boundary_no_boundary_returns_file_size_and_warns(pgn_path):
    """Window with no `\\n\\n[`: fallback to file_size and flip the
    one-shot warned-oversized flag to True."""
    # A blob with NO `\n\n[` anywhere -- a single oversized "game".
    pgn_path.write_bytes(b"[Event \"x\"]\n1. e4 e5 *  (no boundary here)\n")
    records, cb, _ = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    assert tailer._warned_oversized is False
    file_size = pgn_path.stat().st_size
    out = tailer._snap_to_boundary(0, file_size, file_size)
    assert out == file_size
    assert tailer._warned_oversized is True


def test_snap_to_boundary_resets_warned_flag_when_boundary_returns(pgn_path):
    """After a fallback (warned=True), the next successful snap must
    clear _warned_oversized back to False so a later regression warns again."""
    records, cb, _ = _records_collector()
    tailer = PgnTailer(pgn_path, cb)
    # Force the flag set as if a prior poll hit the fallback path.
    tailer._warned_oversized = True

    _write_pgn_text(pgn_path, _ONE_GAME + _SECOND_GAME)
    file_size = pgn_path.stat().st_size
    tailer._snap_to_boundary(0, file_size, file_size)
    assert tailer._warned_oversized is False


def test_snap_to_boundary_oserror_returns_end(pgn_path, monkeypatch):
    """A read failure during snap falls back to `end` (best-effort);
    the caller will then re-attempt and the parser will skip mid-game garbage."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _ = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    def boom(self, *_a, **_kw):
        raise OSError("disk gone")
    monkeypatch.setattr(Path, "open", boom)

    file_size = pgn_path.stat().st_size
    out = tailer._snap_to_boundary(0, file_size, file_size)
    assert out == file_size  # `end` is what was passed in; both match here


def test_parse_delta_returns_empty_on_oserror(pgn_path, monkeypatch):
    """A read failure on the PGN file must be swallowed: empty records,
    offset unchanged, no exception escapes."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    def boom(self, *_a, **_kw):
        raise OSError("disk gone")
    monkeypatch.setattr(Path, "open", boom)

    out, new_offset = tailer._parse_delta(0, pgn_path.stat().st_size)
    assert out == []
    assert new_offset == 0  # offset must NOT advance on read failure


def test_parse_delta_returns_empty_on_read_game_exception(pgn_path, monkeypatch):
    """If chess.pgn.read_game raises, the tailer logs and breaks out --
    it does not advance the offset or surface the exception."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    import chess.pgn as pgn_mod

    def boom(*_a, **_kw):
        raise RuntimeError("parser exploded")
    monkeypatch.setattr(pgn_mod, "read_game", boom)

    out, new_offset = tailer._parse_delta(0, pgn_path.stat().st_size)
    assert out == []
    # Offset must NOT advance: no game was successfully parsed.
    assert new_offset == 0


@pytest.mark.asyncio
async def test_no_change_is_skipped(pgn_path):
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n1 = await tailer.poll_once()
    assert n1 == 1

    # Same mtime + same size + same offset -> fast path.
    n2 = await tailer.poll_once()
    assert n2 == 0
    assert len(records) == 1


@pytest.mark.asyncio
async def test_truncation_resets_offset(pgn_path):
    """Defensive: if the file shrinks, reset and reparse from 0. Covers
    test fixtures, manual edits, and a future runner that rotates."""
    pgn_path.write_text(_ONE_GAME + _SECOND_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
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
    The first poll must parse the whole file, not wait for an append."""
    pgn_path.write_text(_ONE_GAME + _SECOND_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    n = await tailer.poll_once()
    assert n == 2
    assert [r.game_n for r in records] == [1, 2]


@pytest.mark.asyncio
async def test_offset_advances_monotonically(pgn_path):
    """Guard against a regression where ``_offset`` resets every poll
    and forces a full re-parse of the PGN -- that turned `pgn_stats`
    sluggish historically, and the same shape of bug in the tailer
    would do worse: re-parse the *full* movetree on every poll, not
    just headers. Truncation is the only legal reset path; a normal
    append must never shrink the offset."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    await tailer.poll_once()
    off1 = tailer.offset
    assert off1 > 0

    # Append a second game; offset must grow, not reset.
    with pgn_path.open("a", encoding="utf-8") as f:
        f.write(_SECOND_GAME)
    _bump_mtime(pgn_path)

    await tailer.poll_once()
    off2 = tailer.offset
    assert off2 > off1

    # Idempotent poll on unchanged file; offset must hold.
    await tailer.poll_once()
    assert tailer.offset == off2

    # Append a third complete game (reuse the second's bytes; distinct
    # round number, doesn't matter for this guard).
    with pgn_path.open("a", encoding="utf-8") as f:
        f.write(_SECOND_GAME)
    _bump_mtime(pgn_path)

    await tailer.poll_once()
    assert tailer.offset > off2
    assert len(records) == 3


# ---------------------------------------------------------------------------
# Lifecycle (start / stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop(pgn_path):
    """`start` spawns the task; `stop` cancels and joins. Idempotent on
    both ends."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, received = _records_collector()
    tailer = PgnTailer(pgn_path, cb, poll_interval=0.05)

    await tailer.start()
    assert tailer.is_running()
    # Idempotent: a second start is a no-op.
    await tailer.start()
    assert tailer.is_running()

    # Wait for the immediate-pass parse to deliver the first record.
    await received.wait()

    await tailer.stop()
    assert not tailer.is_running()
    # Stop is idempotent.
    await tailer.stop()
    assert len(records) >= 1


@pytest.mark.asyncio
async def test_loop_stays_responsive_during_slow_parse(pgn_path):
    """Synthetic 200ms parse must not block other coroutines on the
    loop. We schedule a 50ms heartbeat alongside poll_once and assert
    the heartbeat ticked while the parse was still in flight."""
    import time as _t

    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    real_parse = tailer._parse_delta
    def slow_parse(start, end):
        _t.sleep(0.2)
        return real_parse(start, end)
    tailer._parse_delta = slow_parse

    ticks = 0
    async def heartbeat():
        nonlocal ticks
        for _ in range(8):
            await asyncio.sleep(0.025)
            ticks += 1

    poll_task = asyncio.create_task(tailer.poll_once())
    hb_task = asyncio.create_task(heartbeat())
    await asyncio.gather(poll_task, hb_task)
    assert len(records) == 1
    assert ticks >= 5  # at minimum, heartbeat did real work during the parse


@pytest.mark.asyncio
async def test_parse_runs_off_main_loop_thread(pgn_path):
    """Multi-MB PGN deltas must not block the asyncio loop. The parse
    is dispatched via asyncio.to_thread; verify the actual call lands
    on a worker thread, not the main thread that ran poll_once."""
    import threading

    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)

    main_thread = threading.get_ident()
    parse_thread: list[int] = []
    real_parse = tailer._parse_delta

    def spy_parse(start, end):
        parse_thread.append(threading.get_ident())
        return real_parse(start, end)
    tailer._parse_delta = spy_parse

    await tailer.poll_once()
    assert len(records) == 1
    assert len(parse_thread) == 1
    assert parse_thread[0] != main_thread


@pytest.mark.asyncio
async def test_callback_exceptions_dont_kill_tailer(pgn_path):
    """One bad callback must not stop the loop -- log + carry on. Slice
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


# ---------------------------------------------------------------------------
# finalize(): teardown-mode drain to EOF
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_paused_drains_existing_pgn(pgn_path):
    """Tailer that was never started (gated off, no subscribers): finalize
    runs poll_once from the caller's context to catch everything fastchess
    wrote during the pause."""
    pgn_path.write_text(_ONE_GAME + _SECOND_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)
    assert not tailer.is_running()

    await tailer.finalize()

    assert len(records) == 2
    assert records[0].white == "Engine A"
    assert records[1].white == "Engine B"
    assert tailer.offset == pgn_path.stat().st_size


@pytest.mark.asyncio
async def test_finalize_running_exits_after_eof(pgn_path):
    """Running tailer + finalize: run loop exits cleanly once caught up
    to EOF. No external stop signal needed."""
    pgn_path.write_text(_ONE_GAME, encoding="utf-8")
    records, cb, received = _records_collector()
    tailer = PgnTailer(pgn_path, cb, poll_interval=0.05)
    await tailer.start()
    # Wait for the initial poll to deliver the record; the run loop is
    # in steady state after the first callback fires.
    await received.wait()
    assert tailer.is_running()

    await tailer.finalize()

    assert not tailer.is_running()
    assert len(records) == 1


@pytest.mark.asyncio
async def test_finalize_running_drains_capped_backlog(pgn_path, monkeypatch):
    """With the per-poll cap forced small, a multi-game file requires
    several poll iterations to drain. Finalize must keep going until
    EOF, not exit on the first cap-truncated pass."""
    from sturddle_view.tournament import pgn_tail as pt_mod
    monkeypatch.setattr(pt_mod, "_MAX_DELTA_BYTES_PER_POLL", 200)

    pgn_path.write_text(_ONE_GAME + _SECOND_GAME, encoding="utf-8")
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb, poll_interval=0.05)
    await tailer.start()

    await tailer.finalize()

    assert not tailer.is_running()
    assert len(records) == 2
    assert tailer.offset == pgn_path.stat().st_size


@pytest.mark.asyncio
async def test_finalize_paused_no_file_is_noop(pgn_path):
    """No file written, no run loop: finalize should not raise."""
    records, cb, _received = _records_collector()
    tailer = PgnTailer(pgn_path, cb)
    await tailer.finalize()
    assert records == []
    assert tailer.offset == 0
