"""Perf test harness: CLI option and baseline helpers.

Two comparison modes:

- `bench_compare(key, seconds, tolerance)`: absolute seconds vs the
  stored baseline. Use for I/O-bound benches where the calibrator
  (which is CPU-bound) cannot normalize host load meaningfully.
- `bench_compare_ratio(key, seconds, tolerance)`: divides the measured
  time by a session-cached host-speed calibrator (a fixed CPU loop) and
  compares against the stored ratio. Cancels host-wide CPU slowdowns
  (background load, thermal throttle, AV scan) so the same bench gives
  a stable verdict across machines and runs.

Both modes read/write the same `baselines.json`. The stored number is
absolute seconds for `bench_compare`, dimensionless ratio for
`bench_compare_ratio` -- the key namespace tells the maintainer which.
"""
from __future__ import annotations

import json
import pathlib
import timeit

import pytest

BASELINES_PATH = pathlib.Path(__file__).parent / "baselines.json"
UPDATE_FLAG = "--update-perf-baselines"

# Calibrator: fixed CPU workload. Stable across Python versions and
# CPUs; the absolute time scales with host speed, so its `min` over a
# few runs is a reliable host-speed proxy.
_CALIB_LOOP = "sum(range(100))"
_CALIB_ITERATIONS = 2000
_CALIB_REPEAT = 5


def pytest_addoption(parser):
    parser.addoption(
        UPDATE_FLAG,
        action="store_true",
        default=False,
        help="Rewrite baselines.json with current timings instead of comparing.",
    )


@pytest.fixture(scope="session")
def perf_baselines(request):
    """Load baselines.json once; flush once at session teardown when updating."""
    data: dict = {}
    if BASELINES_PATH.exists():
        data = json.loads(BASELINES_PATH.read_text())
    yield data
    if request.config.getoption(UPDATE_FLAG, default=False):
        BASELINES_PATH.write_text(json.dumps(data, indent=2) + "\n")


@pytest.fixture(scope="session")
def host_speed() -> float:
    """Time `_CALIB_LOOP` x `_CALIB_ITERATIONS`; take min over
    `_CALIB_REPEAT` runs. Smaller is faster -- this is the host-speed
    denominator for ratio comparisons.
    """
    return min(timeit.repeat(_CALIB_LOOP, number=_CALIB_ITERATIONS, repeat=_CALIB_REPEAT))


@pytest.fixture
def bench_compare(request, perf_baselines):
    """Absolute-seconds comparison. Use for I/O-bound benches."""
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


@pytest.fixture
def bench_compare_ratio(request, perf_baselines, host_speed):
    """Ratio-of-host-speed comparison. Use for CPU-bound benches.

    Compares `seconds / host_speed` against the stored ratio. A
    host-wide slowdown moves both numerator and denominator together,
    leaving the ratio unchanged; a real regression in the bench's code
    path shifts the ratio because host_speed is unaffected.
    """
    updating = request.config.getoption(UPDATE_FLAG, default=False)

    def _compare(key: str, seconds: float, tolerance: float = 0.10) -> None:
        ratio = seconds / host_speed
        if updating:
            perf_baselines[key] = ratio
            return
        if key not in perf_baselines:
            pytest.skip(f"No baseline for '{key}' -- run with {UPDATE_FLAG} first")
        baseline = perf_baselines[key]
        limit = baseline * (1.0 + tolerance)
        assert ratio <= limit, (
            f"Perf regression '{key}': ratio {ratio:.4f} > baseline {baseline:.4f}"
            f" + {tolerance*100:.0f}% ({limit:.4f}); seconds={seconds:.6f},"
            f" host_speed={host_speed:.6f}"
        )

    return _compare
