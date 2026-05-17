"""Perf baseline for pgn_stats.read_game_record before P4 (R6a) refactor."""
from __future__ import annotations

import pathlib

import pytest

from sturddle_view.tournament.pgn_stats import read_game_record
from tests.perf._bench import timeit_best_of

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "perf_200_games.pgn"
ITERATIONS = 3
TOLERANCE = 0.10


def _bench_first():
    read_game_record(FIXTURE, 1)


def _bench_mid():
    read_game_record(FIXTURE, 100)


@pytest.mark.perf
def test_bench_read_game_record(bench_compare):
    elapsed_first = timeit_best_of(_bench_first, ITERATIONS)
    bench_compare("read_game_record_first", elapsed_first, tolerance=TOLERANCE)

    elapsed_mid = timeit_best_of(_bench_mid, ITERATIONS)
    bench_compare("read_game_record_mid", elapsed_mid, tolerance=TOLERANCE)
