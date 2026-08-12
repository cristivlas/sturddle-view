"""Match queue and matching logic for PGN reconciliation.

Pure unit tests for `ReconciliationQueue`. Orchestrator wiring
is exercised in `test_pgn_reconciliation.py`.
"""
from __future__ import annotations

import time


from sturddle_view.tournament.pgn_reconcile import (
    MIN_PLIES_FOR_MATCH,
    RECONCILE_LATE_WARNING_S,
    PendingMatch,
    ReconciliationQueue,
    moves_complete_match,
)
from sturddle_view.tournament.pgn_tail import PgnGameRecord


_ENOUGH = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a6",
          "g8f6", "e1g1", "f8e7", "f1e1", "d7d6"]
assert len(_ENOUGH) >= MIN_PLIES_FOR_MATCH


def _pending(pair_id: str = "pair-1", moves: list[str] | None = None) -> PendingMatch:
    return PendingMatch(
        pair_id=pair_id,
        white_proxy="p-w",
        black_proxy="p-b",
        white_engine="EngA",
        black_engine="EngB",
        uci_moves=list(moves if moves is not None else _ENOUGH),
    )


def _record(moves: list[str] | None = None, n: int = 1, result: str = "1-0") -> PgnGameRecord:
    return PgnGameRecord(
        white="EngA",
        black="EngB",
        result=result,
        termination="normal",
        uci_moves=list(moves if moves is not None else _ENOUGH),
        game_n=n,
        round_tag=str(n),
    )


def test_pending_then_pgn_matches():
    """Captured side is a strict prefix of PGN by 1 ply -- the
    typical case (engine never gets a follow-up `position` after the
    final `bestmove`)."""
    q = ReconciliationQueue()
    captured = list(_ENOUGH)            # 12 plies
    pgn_full = list(_ENOUGH) + ["b1c3"]  # 13 plies
    assert q.add_pending(_pending(moves=captured)) is None
    m = q.add_pgn_record(_record(moves=pgn_full))
    assert m is not None
    assert m.pair_id == "pair-1"
    assert m.result == "1-0"
    assert m.game_n == 1
    assert m.ply_count == len(pgn_full)
    # Both queues empty after a match.
    assert q.pending_count == 0
    assert q.pgn_buffer_count == 0


def test_pgn_then_pending_matches():
    """The PGN side may arrive first if fastchess flushes ahead of
    the orchestrator's dissolution. Order shouldn't matter."""
    q = ReconciliationQueue()
    captured = list(_ENOUGH)
    pgn_full = list(_ENOUGH) + ["b1c3"]
    assert q.add_pgn_record(_record(moves=pgn_full)) is None
    m = q.add_pending(_pending(moves=captured))
    assert m is not None
    assert m.pair_id == "pair-1"


def test_exact_equal_lists_match():
    """Sanity: if the engine *did* receive a follow-up position after
    its last bestmove (rare but legal), our captured list equals the
    PGN list and matching still succeeds."""
    q = ReconciliationQueue()
    moves = list(_ENOUGH)
    q.add_pending(_pending(moves=moves))
    m = q.add_pgn_record(_record(moves=moves))
    assert m is not None


def test_captured_prefix_two_plies_short_matches():
    """fastchess can adjudicate one move ahead, leaving us 2 plies
    short relative to the PGN (e.g. the deciding move and the
    response). Still a match."""
    q = ReconciliationQueue()
    captured = list(_ENOUGH)            # 12 plies
    pgn_full = list(_ENOUGH) + ["b1c3", "d8d7"]  # 14 plies
    q.add_pending(_pending(moves=captured))
    m = q.add_pgn_record(_record(moves=pgn_full))
    assert m is not None


def test_captured_overrun_by_one_matches():
    """Adjudication-after-bestmove case: the engine emitted bestmove
    so we appended it to the captured list, but fastchess adjudicated
    and never wrote that final move into the PGN. Captured is one ply
    longer than PGN -- still a match."""
    q = ReconciliationQueue()
    captured = list(_ENOUGH) + ["b1c3"]  # 13 plies
    pgn_short = list(_ENOUGH)            # 12 plies
    q.add_pending(_pending(moves=captured))
    m = q.add_pgn_record(_record(moves=pgn_short))
    assert m is not None


def test_captured_overrun_by_two_does_not_match():
    """Larger overrun means the captured list has moves the PGN
    doesn't -- treat as real divergence, not a tail-truncation."""
    q = ReconciliationQueue()
    captured = list(_ENOUGH) + ["b1c3", "d8d7"]
    pgn_short = list(_ENOUGH)
    q.add_pending(_pending(moves=captured))
    assert q.add_pgn_record(_record(moves=pgn_short)) is None
    assert q.pending_count == 1
    assert q.pgn_buffer_count == 1


def test_internal_divergence_does_not_match():
    """Same length, identical opening, divergence in the middle. Must
    not match -- backwards walk catches it."""
    q = ReconciliationQueue()
    captured = list(_ENOUGH)
    pgn = list(_ENOUGH)
    pgn[6] = "h2h4"  # diverge at ply 6
    q.add_pending(_pending(moves=captured))
    assert q.add_pgn_record(_record(moves=pgn)) is None


def test_overrun_by_one_with_divergence_in_compared_range_does_not_match():
    """Captured overruns PGN by 1 ply AND has a real divergence inside
    the compared range. Must NOT match. Pins the precedence of
    `cmp_end = n - 1 - max(0, overrun)` against `>>` mutations that
    parse as `(n - 1) >> max(0, overrun)`, which would shrink cmp_end
    enough to skip past the divergence."""
    captured = list(_ENOUGH) + ["b1c3"]  # 13 plies (overrun by 1)
    pgn = list(_ENOUGH)                    # 12 plies
    pgn[7] = "XXXX"                        # divergence at index 7
    q = ReconciliationQueue()
    q.add_pending(_pending(moves=captured))
    assert q.add_pgn_record(_record(moves=pgn)) is None


def test_divergence_at_index_zero_does_not_match():
    """First-ply divergence is caught by the backwards walk. Pins the
    range stop value `-1` against `-0`/`+1` NumberReplacer mutations
    that would change the range to exclude index 0."""
    captured = ["XXXX"] + _ENOUGH[1:]
    pgn = list(_ENOUGH)
    q = ReconciliationQueue()
    q.add_pending(_pending(moves=captured))
    assert q.add_pgn_record(_record(moves=pgn)) is None


def test_below_min_plies_does_not_match():
    """Short games skip reconciliation entirely. The pending side is
    silently dropped (no entry queued); a too-short PGN record is also
    discarded."""
    q = ReconciliationQueue()
    short_moves = _ENOUGH[:5]
    assert q.add_pending(_pending(moves=short_moves)) is None
    assert q.pending_count == 0  # not buffered
    assert q.add_pgn_record(_record(moves=short_moves)) is None
    assert q.pgn_buffer_count == 0


def test_non_matching_move_lists_stay_pending():
    q = ReconciliationQueue()
    other = list(_ENOUGH)
    other[-1] = "h7h6"  # diverge at the last ply

    assert q.add_pending(_pending(moves=_ENOUGH)) is None
    assert q.add_pgn_record(_record(moves=other)) is None
    assert q.pending_count == 1
    assert q.pgn_buffer_count == 1


def test_two_pending_matches_in_arrival_order():
    q = ReconciliationQueue()
    moves_a = list(_ENOUGH)
    moves_b = list(_ENOUGH)
    moves_b[-1] = "h7h6"  # distinct

    q.add_pending(_pending(pair_id="A", moves=moves_a))
    q.add_pending(_pending(pair_id="B", moves=moves_b))

    m1 = q.add_pgn_record(_record(moves=moves_b, n=1))
    assert m1 is not None and m1.pair_id == "B"

    m2 = q.add_pgn_record(_record(moves=moves_a, n=2))
    assert m2 is not None and m2.pair_id == "A"

    assert q.pending_count == 0


def test_timeout_drops_pending(monkeypatch):
    q = ReconciliationQueue(timeout_s=0.5)
    q.add_pending(_pending())
    assert q.pending_count == 1

    # Advance time past the timeout.
    real = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real + 1.0)
    dropped = q.sweep()
    assert len(dropped) == 1
    assert dropped[0].pair_id == "pair-1"
    assert q.pending_count == 0


def test_sweep_does_not_drop_when_age_below_timeout(monkeypatch):
    """When (now - enqueued_at) < timeout_s, sweep keeps the entry.
    Pin now ~= enqueued_at so the *subtraction* yields a tiny number;
    a buggy `+` or `*` substitute would produce a huge age and over-drop."""
    q = ReconciliationQueue(timeout_s=0.5)
    entry = _pending()
    # Force enqueued_at to a known large value matching `now`.
    entry.enqueued_at = 1_000_000.0
    q._pending.append(entry)

    monkeypatch.setattr(time, "monotonic", lambda: 1_000_000.0 + 0.1)  # age=0.1s
    dropped = q.sweep()
    assert dropped == []
    assert q.pending_count == 1


def test_sweep_does_not_drop_at_exact_timeout(monkeypatch):
    """Age equal to timeout_s is NOT dropped (strict `>`).
    Catches Gt -> GtE / NotEq / IsNot mutations on the comparison."""
    q = ReconciliationQueue(timeout_s=0.5)
    entry = _pending()
    entry.enqueued_at = 1_000.0
    q._pending.append(entry)

    monkeypatch.setattr(time, "monotonic", lambda: 1_000.0 + 0.5)  # age == timeout
    dropped = q.sweep()
    assert dropped == []
    assert q.pending_count == 1


def test_sweep_two_entries_drops_only_old(monkeypatch):
    """Pin the FIFO order: first entry expired, second fresh. Sweep stops
    at the second. Kills index-mutations on `_pending[0]` (item[1]/[-1]
    would point at the wrong entry's enqueued_at)."""
    q = ReconciliationQueue(timeout_s=0.5)
    old = _pending(pair_id="old")
    old.enqueued_at = 1_000.0
    fresh = _pending(pair_id="fresh")
    fresh.enqueued_at = 1_100.0  # 100s newer
    q._pending.append(old)
    q._pending.append(fresh)

    # now = 1_000 + 1.0  -> old.age=1.0 (drop), fresh.age=-99.0 (keep)
    monkeypatch.setattr(time, "monotonic", lambda: 1_000.0 + 1.0)
    dropped = q.sweep()
    assert len(dropped) == 1
    assert dropped[0].pair_id == "old"
    assert q.pending_count == 1


def test_sweep_does_not_drop_pgn_records(monkeypatch):
    """PGN-side records must persist regardless of age: when a slot
    finishes all its assigned games, the pair stays alive (no
    ucinewgame follows) for the rest of the tournament. The matching
    dissolution can arrive arbitrarily long after the PGN flush."""
    q = ReconciliationQueue(timeout_s=0.5)
    q.add_pgn_record(_record())
    assert q.pgn_buffer_count == 1

    real = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real + 1.0)
    q.sweep()
    assert q.pgn_buffer_count == 1


def test_clear_drops_everything():
    q = ReconciliationQueue()
    q.add_pending(_pending())
    q.add_pgn_record(_record(moves=_ENOUGH[:-1] + ["h7h6"]))  # non-match
    assert q.pending_count == 1 and q.pgn_buffer_count == 1
    q.clear()
    assert q.pending_count == 0 and q.pgn_buffer_count == 0


def test_queue_max_evicts_oldest():
    """Bounded deques drop the oldest on overflow. With a sane cap and
    realistic tournament size we never see this in practice; here just
    verify the cap is wired."""
    q = ReconciliationQueue(queue_max=3)
    for i in range(5):
        moves = list(_ENOUGH)
        moves[0] = f"a{i}a{i}"  # distinct so they don't accidentally match
        # Engines won't actually emit "a0a0" but the matcher only
        # cares about list equality, so any unique sentinel works.
        q.add_pending(_pending(pair_id=f"P{i}", moves=moves))
    assert q.pending_count == 3


def test_env_float_non_numeric_returns_default(monkeypatch):
    """`_env_float` catches ValueError from float() on non-numeric env
    var and falls back to default. Kills ExceptionReplacer mutations
    on the `except ValueError` catch (which would let the parse error
    propagate)."""
    from sturddle_view.tournament.pgn_reconcile import _env_float
    monkeypatch.setenv("SV_TEST_BAD_FLOAT", "not-a-number")
    assert _env_float("SV_TEST_BAD_FLOAT", 42.0) == 42.0


def test_env_int_non_numeric_returns_default(monkeypatch):
    """`_env_int` mirror of _env_float test. Kills ExceptionReplacer
    on its `except ValueError` catch."""
    from sturddle_view.tournament.pgn_reconcile import _env_int
    monkeypatch.setenv("SV_TEST_BAD_INT", "not-an-int")
    assert _env_int("SV_TEST_BAD_INT", 17) == 17


def test_late_match_emits_warning(monkeypatch, caplog):
    """Pending entry sat past LATE_WARNING_S before matching -- INFO log.
    Set enqueued_at and now explicitly so that
    `now - enqueued_at != now % enqueued_at`, killing the `-` -> `%`
    mutation on the age computation (which coincides with subtraction
    when the dividend is just over the divisor, the typical case for
    consecutive `time.monotonic()` reads)."""
    q = ReconciliationQueue()
    entry = _pending()
    entry.enqueued_at = 2.0  # small base so `now % enqueued_at != now - enqueued_at`
    q._pending.append(entry)

    # now=7.1, enqueued_at=2.0 -> age original 5.1; mutated 7.1 % 2.0 = 1.1.
    monkeypatch.setattr(time, "monotonic", lambda: 2.0 + RECONCILE_LATE_WARNING_S + 0.1)
    with caplog.at_level("INFO", logger="sturddle_view.tournament.pgn_reconcile"):
        m = q.add_pgn_record(_record())
    assert m is not None
    assert any("reconcile late" in r.message for r in caplog.records)


def test_fresh_match_does_not_warn(caplog):
    """Sub-threshold latency stays silent."""
    q = ReconciliationQueue()
    q.add_pending(_pending())
    with caplog.at_level("INFO", logger="sturddle_view.tournament.pgn_reconcile"):
        m = q.add_pgn_record(_record())
    assert m is not None
    assert not any("reconcile late" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# moves_complete_match: whole-game guard for dissolve-on-PGN-record
# ---------------------------------------------------------------------------


def test_complete_match_exact():
    assert moves_complete_match(list(_ENOUGH), list(_ENOUGH))


def test_complete_match_below_min_plies():
    short = _ENOUGH[: MIN_PLIES_FOR_MATCH - 1]
    assert not moves_complete_match(short, short)


def test_complete_match_shortfall_boundary():
    """Captured may trail the PGN by up to 2 plies; more means the pair
    is likely mid-game on the same line (e.g. a rematch)."""
    full = _ENOUGH + ["c2c3", "e8g8", "h2h3"]  # 15 plies
    assert moves_complete_match(full[:-2], full)
    assert not moves_complete_match(full[:-3], full)


def test_complete_match_overrun_boundary():
    """Adjudication: captured may overrun the PGN by exactly 1 ply."""
    assert moves_complete_match(_ENOUGH + ["c2c3"], list(_ENOUGH))
    assert not moves_complete_match(_ENOUGH + ["c2c3", "e8g8"], list(_ENOUGH))


def test_complete_match_divergent_moves():
    other = list(_ENOUGH)
    other[-1] = "h7h6"
    assert not moves_complete_match(other, list(_ENOUGH))
