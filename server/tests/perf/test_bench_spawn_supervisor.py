"""Perf gate for engine spawn (R3 / P6).

Baseline captured against HVE._spawn_engine BEFORE the refactor. After
the refactor, tests that EngineSupervisor.spawn stays within tolerance.

Drives a real subprocess spawn against the fake UCI engine fixture so
the bench measures process creation + UCI handshake + configure, which
is what the refactor must not regress.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine

ROUNDS = 50
TOLERANCE = 0.10
_FAKE_ENGINE = pathlib.Path(__file__).parent.parent / "fixtures" / "fake_uci_engine.py"


def _make_hve() -> HumanVsEngine:
    return HumanVsEngine(
        engine_path=sys.executable,
        bus=EventBus(),
    )


async def _spawn_and_quit(hve: HumanVsEngine) -> None:
    hve._engine_args = [str(_FAKE_ENGINE)]
    engine = await hve._spawn_engine()
    try:
        await engine.quit()
    except Exception:
        pass


def _bench_one_spawn() -> float:
    hve = _make_hve()
    loop = asyncio.new_event_loop()
    try:
        t0 = time.perf_counter_ns()
        loop.run_until_complete(_spawn_and_quit(hve))
        return (time.perf_counter_ns() - t0) / 1e9
    finally:
        loop.close()


@pytest.mark.perf
def test_bench_spawn_supervisor_vs_inline(bench_compare):
    samples = sorted(_bench_one_spawn() for _ in range(ROUNDS))
    median = samples[len(samples) // 2]
    bench_compare("spawn_engine_median", median, tolerance=TOLERANCE)