"""Perf gate for board_from (R1).

Baselines captured against inline chess.Board() before refactor.
After refactor, tests that board_from() stays within tolerance.
"""
from __future__ import annotations

import pytest

from sturddle_view.chess.board import board_from

TOLERANCE = 0.10
# Startpos is a sub-microsecond op; even with INNER_LOOPS=1000 amortization
# the per-round measurement (~880 us) sits close to OS scheduler noise floor.
# Median drifts ~7% day-to-day on shared dev machines; 15% gives proper
# headroom for normal variance while still catching real regressions.
TOLERANCE_STARTPOS = 0.15
START_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
# Inner-loop count for sub-microsecond ops so each timed round is well above
# perf_counter resolution. board_from(None) is ~1us; 1000 inner calls
# amortize timer overhead and lift the measurement to ~1ms scale.
INNER_LOOPS = 1000


def _via_board_from_startpos():
    for _ in range(INNER_LOOPS):
        board_from(None)


def _via_board_from_fen():
    for _ in range(INNER_LOOPS):
        board_from(START_FEN)


@pytest.mark.perf
def test_bench_board_from_startpos(benchmark, bench_compare):
    benchmark(_via_board_from_startpos)
    bench_compare("board_from_startpos_inline", benchmark.stats.stats.median, tolerance=TOLERANCE_STARTPOS)


@pytest.mark.perf
def test_bench_board_from_fen(benchmark, bench_compare):
    benchmark(_via_board_from_fen)
    bench_compare("board_from_fen_inline", benchmark.stats.stats.median, tolerance=TOLERANCE)
