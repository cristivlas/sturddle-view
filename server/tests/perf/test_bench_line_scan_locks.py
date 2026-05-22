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

pytestmark = pytest.mark.perf

from sturddle_view.tournament.pgn_stats import (
    _iter_games_keyed,
    _iter_games_uncached,
    rewrite_drop_partial_pairs,
)

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_1k_games.pgn"
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
def test_bench_iter_games_keyed_1k_games(benchmark, bench_compare):
    benchmark(_bench_keyed)
    bench_compare("iter_games_keyed_1k", benchmark.stats.stats.min, tolerance=TOLERANCE)


@pytest.mark.perf
def test_bench_iter_games_uncached_1k_games(benchmark, bench_compare):
    benchmark(_bench_uncached)
    bench_compare("iter_games_uncached_1k", benchmark.stats.stats.min, tolerance=TOLERANCE)


@pytest.mark.perf
@pytest.mark.benchmark(min_rounds=15)
def test_bench_rewrite_partial_pairs_1k_games(benchmark, bench_compare):
    benchmark(_bench_rewrite)
    bench_compare("rewrite_partial_pairs_1k", benchmark.stats.stats.min, tolerance=0.15)
