"""DO NOT TOUCH lock benches for pgn_stats line-scan paths.

These pin the performance of `_iter_games_keyed` and `_iter_games_uncached`
-- both still on the hot path (compute_standings, count_partial_pairs).
Any PR that replaces these regex/line-scan paths with
`chess.pgn.read_game` will regress by ~50x and fail here.
"""
from __future__ import annotations

import pathlib

import pytest

from sturddle_view.tournament.pgn_stats import (
    _iter_games_keyed,
    _iter_games_uncached,
)

pytestmark = pytest.mark.perf

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_1k_games.pgn"
TOLERANCE = 0.10


def _bench_keyed():
    for _ in _iter_games_keyed(FIXTURE):
        pass


def _bench_uncached():
    for _ in _iter_games_uncached(FIXTURE):
        pass


@pytest.mark.perf
def test_bench_iter_games_keyed_1k_games(benchmark, bench_compare_ratio):
    benchmark(_bench_keyed)
    bench_compare_ratio("iter_games_keyed_1k", benchmark.stats.stats.min, tolerance=TOLERANCE)


@pytest.mark.perf
def test_bench_iter_games_uncached_1k_games(benchmark, bench_compare_ratio):
    benchmark(_bench_uncached)
    bench_compare_ratio("iter_games_uncached_1k", benchmark.stats.stats.min, tolerance=TOLERANCE)
