"""Perf bench for pgn_stats.read_game_record (I/O-bound; absolute comparison)."""
from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.perf

from sturddle_view.tournament.pgn_stats import read_game_record

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_200_games.pgn"
TOLERANCE = 0.15


def _bench_first():
    read_game_record(FIXTURE, 1)


def _bench_mid():
    read_game_record(FIXTURE, 100)


@pytest.mark.perf
def test_bench_read_game_record_first(benchmark, bench_compare):
    benchmark(_bench_first)
    bench_compare("read_game_record_first", benchmark.stats.stats.min, tolerance=TOLERANCE)


@pytest.mark.perf
def test_bench_read_game_record_mid(benchmark, bench_compare):
    benchmark(_bench_mid)
    bench_compare("read_game_record_mid", benchmark.stats.stats.min, tolerance=TOLERANCE)
