"""Characterization tests for HVE clock behavior -- no extraction yet.

Pins the current behavior of the clock methods embedded in HumanVsEngine so
that later ChessClock extraction (P7) can verify byte-comparable semantics.
All tests drive HVE directly; no clock class exists yet.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl

INITIAL = 60.0
INCREMENT = 5.0
TC = TimeControl(INITIAL, INCREMENT)
TC_NO_INC = TimeControl(INITIAL, 0.0)


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve():
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


async def test_hve_clock_remaining_white_idle_constant(hve):
    """human=black => engine=white is to move; black (human) clock must not tick."""
    await hve.new_game(human_white=False, tc=TC_NO_INC)
    # board.turn==WHITE (engine); black is idle.
    r1 = hve._remaining(chess.BLACK)
    await asyncio.sleep(0.05)
    r2 = hve._remaining(chess.BLACK)
    assert r1 == r2 == INITIAL


async def test_hve_clock_remaining_black_thinking_decrements(hve):
    """The side to move's remaining time must decrease while thinking."""
    await hve.new_game(human_white=False, tc=TC_NO_INC)
    # board.turn==WHITE (engine); white clock is ticking.
    r1 = hve._remaining(chess.WHITE)
    await asyncio.sleep(0.05)
    r2 = hve._remaining(chess.WHITE)
    assert r2 < r1


async def test_hve_consume_turn_subtracts_elapsed_and_adds_increment(hve):
    """_consume_turn_time debits elapsed and credits increment to side-to-move."""
    await hve.new_game(human_white=True, tc=TC)
    # Simulate 1 second elapsed on white's turn.
    hve._clock.turn_started_at = time.monotonic() - 1.0
    before = hve._clock.white_time
    hve._consume_turn_time()
    after = hve._clock.white_time
    # Lost ~1s, gained INCREMENT.
    delta = after - before
    assert abs(delta - (INCREMENT - 1.0)) < 0.05


async def test_hve_clock_history_one_entry_per_ply(hve):
    """_clock_history must have exactly len(move_stack) entries at all times."""
    await hve.new_game(human_white=True, tc=TC)
    assert len(hve._clock.history) == len(hve._board.move_stack)
    await hve.submit_move("e2e4")
    assert len(hve._clock.history) == len(hve._board.move_stack)


async def test_hve_clock_pop_on_takeback_restores_prior_clocks(hve):
    """Takeback must restore both clocks to the snapshot before the move."""
    await hve.new_game(human_white=True, tc=TC_NO_INC)
    white_before = hve._clock.white_time
    black_before = hve._clock.black_time

    await hve.submit_move("e2e4")
    # Engine stub doesn't play; board.turn==BLACK (engine's turn). Pop one ply.
    await hve.takeback()

    assert abs(hve._clock.white_time - white_before) < 0.1
    assert abs(hve._clock.black_time - black_before) < 0.1


async def test_hve_clock_pause_freezes_no_increment(hve):
    """Pause bakes elapsed without adding increment; remaining stays frozen after."""
    await hve.new_game(human_white=True, tc=TC)
    await asyncio.sleep(0.05)
    await hve.pause()

    frozen = hve._clock.white_time
    assert frozen < INITIAL  # elapsed was deducted, no increment

    r1 = hve._remaining(chess.WHITE)
    await asyncio.sleep(0.05)
    r2 = hve._remaining(chess.WHITE)
    assert r1 == r2 == frozen


async def test_hve_clock_seed_from_pgn_pads_missing_with_initial(hve):
    """Mismatched seed_clock_history falls back to (initial, initial) per ply."""
    seed_history = [(50.0, 55.0)]  # 1 entry for 2 plies -- mismatch
    await hve.new_game(
        human_white=True,
        tc=TC,
        start_moves_uci=["e2e4", "e7e5"],
        seed_clock_history=seed_history,
    )
    assert len(hve._clock.history) == 2
    for w, b in hve._clock.history:
        assert w == TC.initial_seconds
        assert b == TC.initial_seconds
