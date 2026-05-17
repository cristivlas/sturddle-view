"""DO NOT TOUCH lock benches for pgn_stats line-scan paths (R6 / P4).

These benches pin the performance of _iter_games_keyed, _iter_games_uncached,
and rewrite_drop_partial_pairs. Any PR that replaces these regex/line-scan
paths with chess.pgn.read_game will regress by ~50x and fail here.
"""
from __future__ import annotations

import pathlib
import shutil
import tempfile

import pytest

from sturddle_view.tournament.pgn_stats import (
    _iter_games_keyed,
    _iter_games_uncached,
    rewrite_drop_partial_pairs,
)
from tests.perf._bench import timeit_best_of

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_1k_games.pgn"
ITERATIONS = 5
TOLERANCE = 0.10


def _bench_keyed():
    for _ in _iter_games_keyed(FIXTURE):
        pass


def _bench_uncached():
    for _ in _iter_games_uncached(FIXTURE):
        pass


def _bench_rewrite():
    with tempfile.NamedTemporaryFile(suffix=".pgn", delete=False) as tf:
        tmp = pathlib.Path(tf.name)
    shutil.copy(FIXTURE, tmp)
    try:
        rewrite_drop_partial_pairs(tmp)
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.perf
def test_bench_iter_games_keyed_1k_games(bench_compare):
    elapsed = timeit_best_of(_bench_keyed, ITERATIONS)
    bench_compare("iter_games_keyed_1k", elapsed, tolerance=TOLERANCE)


@pytest.mark.perf
def test_bench_iter_games_uncached_1k_games(bench_compare):
    elapsed = timeit_best_of(_bench_uncached, ITERATIONS)
    bench_compare("iter_games_uncached_1k", elapsed, tolerance=TOLERANCE)


@pytest.mark.perf
def test_bench_rewrite_partial_pairs_1k_games(bench_compare):
    elapsed = timeit_best_of(_bench_rewrite, ITERATIONS)
    bench_compare("rewrite_partial_pairs_1k", elapsed, tolerance=TOLERANCE)
