"""Perf test harness: marker registration, env-var skip, CLI option, baseline helpers."""
from __future__ import annotations

import json
import os
import pathlib

import pytest

BASELINES_PATH = pathlib.Path(__file__).parent / "baselines.json"
PERF_MARKER = "perf"
ENV_VAR = "SV_RUN_PERF_BENCHES"
UPDATE_FLAG = "--update-perf-baselines"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{PERF_MARKER}: mark a test as a performance benchmark (skipped unless {ENV_VAR}=1)",
    )


def pytest_addoption(parser):
    parser.addoption(
        UPDATE_FLAG,
        action="store_true",
        default=False,
        help="Rewrite baselines.json with current timings instead of comparing.",
    )


def pytest_collection_modifyitems(config, items):
    if os.environ.get(ENV_VAR) == "1":
        return
    skip = pytest.mark.skip(reason=f"{ENV_VAR} not set")
    for item in items:
        if item.get_closest_marker(PERF_MARKER):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def perf_baselines(request):
    """Load baselines.json once; flush once at session teardown when updating."""
    data: dict = {}
    if BASELINES_PATH.exists():
        data = json.loads(BASELINES_PATH.read_text())
    yield data
    if request.config.getoption(UPDATE_FLAG, default=False):
        BASELINES_PATH.write_text(json.dumps(data, indent=2) + "\n")


@pytest.fixture
def bench_compare(request, perf_baselines):
    """Return a callable: bench_compare(key, seconds, tolerance=0.10).

    In update mode records the timing; in compare mode asserts within tolerance.
    """
    updating = request.config.getoption(UPDATE_FLAG, default=False)

    def _compare(key: str, seconds: float, tolerance: float = 0.10) -> None:
        if updating:
            perf_baselines[key] = seconds
            return
        if key not in perf_baselines:
            pytest.skip(f"No baseline for '{key}' -- run with {UPDATE_FLAG} first")
        baseline = perf_baselines[key]
        limit = baseline * (1.0 + tolerance)
        assert seconds <= limit, (
            f"Perf regression '{key}': {seconds:.6f}s > baseline {baseline:.6f}s"
            f" + {tolerance*100:.0f}% ({limit:.6f}s)"
        )

    return _compare
