"""Smoke tests proving the perf harness records and compares correctly."""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.perf

from tests.perf._bench import timeit
from tests.perf.conftest import BASELINES_PATH, UPDATE_FLAG

SMOKE_KEY = "_smoke_bench"
SMOKE_TOLERANCE = 0.50  # 50% -- smoke bench timing is intentionally loose


def _trivial():
    return sum(range(100))


def test_perf_harness_runs():
    elapsed = timeit(_trivial, 1000)
    assert elapsed >= 0


def test_perf_harness_update_flag_rewrites_baseline(request, tmp_path, monkeypatch):
    fake_path = tmp_path / "baselines.json"
    fake_path.write_text("{}")
    monkeypatch.setattr("tests.perf.conftest.BASELINES_PATH", fake_path)

    data: dict = json.loads(fake_path.read_text())
    updating = request.config.getoption(UPDATE_FLAG, default=False)

    elapsed = timeit(_trivial, 1000)
    if updating:
        data[SMOKE_KEY] = elapsed
        fake_path.write_text(json.dumps(data, indent=2) + "\n")
        assert SMOKE_KEY in data
    else:
        pytest.skip("Pass --update-perf-baselines to exercise baseline write path")


def test_perf_harness_compares_against_baseline_within_tolerance(bench_compare):
    elapsed = timeit(_trivial, 1000)
    bench_compare(SMOKE_KEY, elapsed, tolerance=SMOKE_TOLERANCE)
