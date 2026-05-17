"""Perf baseline for board_from before R1 refactor.

Baselines the CURRENT inline chess.Board(fen) if fen else chess.Board()
pattern so the extracted board_from() helper can be compared against it.
"""
from __future__ import annotations

import chess
import pytest

from tests.perf._bench import timeit_best_of

ITERATIONS = 100_000
START_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"


def _inline_startpos():
    return chess.Board()


def _inline_from_fen():
    return chess.Board(START_FEN)


@pytest.mark.perf
def test_bench_board_from_vs_inline(bench_compare):
    elapsed_startpos = timeit_best_of(_inline_startpos, ITERATIONS)
    bench_compare("board_from_startpos_inline", elapsed_startpos, tolerance=0.05)

    elapsed_fen = timeit_best_of(_inline_from_fen, ITERATIONS)
    bench_compare("board_from_fen_inline", elapsed_fen, tolerance=0.05)
