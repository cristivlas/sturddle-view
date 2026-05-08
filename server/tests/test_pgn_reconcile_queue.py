"""Match queue and matching logic for PGN reconciliation (slice 3).

Pure unit tests for `ReconciliationQueue` — the orchestrator wiring
is exercised in `test_pgn_reconciliation.py`.
"""
from __future__ import annotations

import time

import pytest

from sturddle_view.tournament.pgn_reconcile import (
    MIN_PLIES_FOR_MATCH,
    PendingMatch,
    ReconciliationQueue,
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


def test_timeout_drops_pgn_buffer(monkeypatch):
    q = ReconciliationQueue(timeout_s=0.5)
    q.add_pgn_record(_record())
    assert q.pgn_buffer_count == 1

    real = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real + 1.0)
    q.sweep()
    assert q.pgn_buffer_count == 0


def test_clear_drops_everything():
    q = ReconciliationQueue()
    q.add_pending(_pending())
    q.add_pgn_record(_record(moves=_ENOUGH[:-1] + ["h7h6"]))  # non-match
    assert q.pending_count == 1 and q.pgn_buffer_count == 1
    q.clear()
    assert q.pending_count == 0 and q.pgn_buffer_count == 0


def test_try_match_now_hits_buffered_pgn():
    """Terminal-teardown path: PGN was already buffered, dissolution
    fires with `terminal=True`, one-shot match succeeds without
    parking the entry."""
    q = ReconciliationQueue()
    captured = list(_ENOUGH)
    pgn_full = list(_ENOUGH) + ["b1c3"]
    assert q.add_pgn_record(_record(moves=pgn_full)) is None
    m = q.try_match_now(_pending(moves=captured))
    assert m is not None
    assert q.pending_count == 0
    assert q.pgn_buffer_count == 0


def test_try_match_now_misses_does_not_park():
    """No PGN buffered (game never finished) -- one-shot returns
    None and the entry is *not* parked for later."""
    q = ReconciliationQueue()
    assert q.try_match_now(_pending()) is None
    assert q.pending_count == 0


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
