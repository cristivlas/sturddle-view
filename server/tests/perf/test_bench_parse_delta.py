"""Perf bench for pgn_tail._parse_delta."""
from __future__ import annotations

import pathlib

import pytest

from sturddle_view.tournament.pgn_tail import PgnTailer

pytestmark = pytest.mark.perf

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_200_games.pgn"
TOLERANCE = 0.10
_FILE_SIZE = FIXTURE.stat().st_size


def _bench_fn():
    PgnTailer(FIXTURE, lambda _: None)._parse_delta(0, _FILE_SIZE)


@pytest.mark.perf
@pytest.mark.benchmark(min_rounds=30)
def test_bench_parse_delta_200_games(benchmark, bench_compare_ratio):
    benchmark(_bench_fn)
    bench_compare_ratio("parse_delta_200_games", benchmark.stats.stats.min, tolerance=TOLERANCE)
