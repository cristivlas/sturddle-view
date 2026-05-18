"""Perf gate for ChessClock.remaining (R4 / P7).

Baseline captured against HVE._remaining BEFORE the refactor. After the
refactor, tests that the extracted ChessClock.remaining (called via the
HVE shim) stays within tolerance.

Pure arithmetic + 4-condition guard; ~50ns/call. Use INNER_LOOPS=1000
so each timed round is well above perf_counter resolution.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl

INNER_LOOPS = 1000


def _make_hve_with_active_clock() -> HumanVsEngine:
    """Drive the ticking-side path: stm == queried side, turn_started_at set.

    Sets attributes under both pre-refactor (HVE-owned) and post-refactor
    (ChessClock-owned) shapes so the bench file survives the swap.
    """
    import time as _t
    hve = HumanVsEngine(engine_path="/nonexistent", bus=EventBus())
    hve._board = chess.Board()
    if hasattr(hve, "_clock"):
        hve._clock.white_time = 300.0
        hve._clock.black_time = 300.0
        hve._clock.start_turn()
    else:
        hve._tc = TimeControl(300.0, 0.0)
        hve._white_time = 300.0
        hve._black_time = 300.0
        hve._turn_started_at = _t.monotonic()
    return hve


def _bench_remaining(hve: HumanVsEngine) -> None:
    for _ in range(INNER_LOOPS):
        hve._remaining(chess.WHITE)


@pytest.mark.perf
def test_bench_clock_remaining(benchmark, bench_compare):
    hve = _make_hve_with_active_clock()
    benchmark(_bench_remaining, hve)
    bench_compare("clock_remaining_inline", benchmark.stats.stats.median)
