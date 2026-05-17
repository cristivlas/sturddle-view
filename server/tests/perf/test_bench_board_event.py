"""Perf gates for HVE._board_event play and view payloads (R9 / P11).

Baselines captured against the pre-refactor monolithic _board_event. After
the extraction, asserts that the assembler + helpers stay within tolerance.

5% tolerance per the test-battery plan; this method is on the per-event hot
path (every move, every tick that touches board state).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl, ViewModeParams

INNER_LOOPS = 100
TC = TimeControl(300.0, 0.0)
PLAY_MOVES = ["e2e4", "e7e5", "g1f3", "b8c6"]
VIEW_MOVES = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"]


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


def _new_hve() -> HumanVsEngine:
    h = HumanVsEngine(engine_path="/nonexistent", bus=EventBus())

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


def _bench_board_event(hve: HumanVsEngine) -> None:
    for _ in range(INNER_LOOPS):
        hve._board_event()


@pytest.mark.perf
def test_bench_board_event_play_payload(benchmark, bench_compare):
    hve = _new_hve()
    asyncio.get_event_loop().run_until_complete(
        hve.new_game(human_white=True, tc=TC, start_moves_uci=PLAY_MOVES)
    )
    benchmark(_bench_board_event, hve)
    bench_compare("board_event_play_payload", benchmark.stats.stats.median, tolerance=0.05)


@pytest.mark.perf
def test_bench_board_event_view_payload(benchmark, bench_compare):
    hve = _new_hve()
    moves_uci = [chess.Move.from_uci(u).uci() for u in VIEW_MOVES]
    asyncio.get_event_loop().run_until_complete(
        hve.enter_view_mode(ViewModeParams(
            start_fen=None,
            moves_uci=moves_uci,
            clock_history=None,
        ))
    )
    asyncio.get_event_loop().run_until_complete(hve.view_goto(3))
    benchmark(_bench_board_event, hve)
    bench_compare("board_event_view_payload", benchmark.stats.stats.median, tolerance=0.05)
