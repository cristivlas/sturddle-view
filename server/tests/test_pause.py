"""Pause/resume behavior on the human's turn.

Tests drive HumanVsEngine without a real UCI engine, mirroring test_takeback.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl
from sturddle_view.play.mode import ModeConflictError


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


async def test_pause_freezes_clock_no_increment(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 5.0))
    await asyncio.sleep(0.05)

    await hve.pause()
    assert hve.is_paused is True
    # Pause must NOT credit the increment (only completed moves do).
    assert hve._clock.white_time < 60.0
    assert hve._clock.white_time > 55.0  # i.e. didn't gain 5s

    frozen_white = hve._clock.white_time
    # While paused, _remaining for the side-to-move stays put.
    await asyncio.sleep(0.10)
    assert hve._remaining(chess.WHITE) == pytest.approx(frozen_white, abs=1e-6)


async def test_resume_restarts_clock(hve):
    await hve.new_game(human_white=True, tc=TimeControl(30.0, 0.0))
    await asyncio.sleep(0.02)
    await hve.pause()
    paused_at = hve._clock.white_time

    await asyncio.sleep(0.05)
    await hve.resume()
    assert hve.is_paused is False
    # Stored clock must not have moved during pause.
    assert hve._clock.white_time == pytest.approx(paused_at, abs=1e-6)

    # Live remaining starts decreasing again after resume.
    await asyncio.sleep(0.05)
    assert hve._remaining(chess.WHITE) < paused_at


async def test_submit_move_rejected_while_paused(hve):
    await hve.new_game(human_white=True, tc=TimeControl(30.0, 0.0))
    await hve.pause()
    with pytest.raises(ModeConflictError):
        await hve.submit_move("e2e4")


async def test_pause_only_on_human_turn(hve):
    # Human is black -> on new_game it is white's (engine's) turn. Pause
    # must refuse.
    await hve.new_game(human_white=False, tc=TimeControl(30.0, 0.0))
    with pytest.raises(RuntimeError, match="your turn"):
        await hve.pause()


async def test_pause_with_no_game_raises(hve):
    with pytest.raises(RuntimeError, match="no active game"):
        await hve.pause()


async def test_double_pause_is_noop(hve):
    await hve.new_game(human_white=True, tc=TimeControl(30.0, 0.0))
    await hve.pause()
    paused_at = hve._clock.white_time
    # Second pause does not double-bake elapsed.
    await hve.pause()
    assert hve._clock.white_time == pytest.approx(paused_at, abs=1e-6)


async def test_set_engine_name_does_not_clear_existing_name(hve):
    """Regression: a fallback _get_hve fetch (no registry name) used to wipe
    the previously-resolved engine_name back to None."""
    hve.set_engine_name("Sturddle 2.5")
    assert hve._engine_name == "Sturddle 2.5"
    hve.set_engine_name(None)
    assert hve._engine_name == "Sturddle 2.5"
    hve.set_engine_name("")
    assert hve._engine_name == "Sturddle 2.5"
    # An explicit non-empty rename DOES take effect.
    hve.set_engine_name("Sturddle 2.6")
    assert hve._engine_name == "Sturddle 2.6"


async def test_new_game_clears_paused(hve):
    await hve.new_game(human_white=True, tc=TimeControl(30.0, 0.0))
    await hve.pause()
    assert hve.is_paused is True
    await hve.new_game(human_white=True, tc=TimeControl(30.0, 0.0))
    assert hve.is_paused is False


async def test_pause_after_game_over_raises(hve, monkeypatch):
    await hve.new_game(human_white=True, tc=TimeControl(30.0, 0.0))
    # Force is_game_over to True without playing a real terminal sequence.
    monkeypatch.setattr(hve._board, "is_game_over", lambda *a, **kw: True)
    with pytest.raises(RuntimeError, match="game is over"):
        await hve.pause()


async def test_clock_tick_carries_paused_flag(hve):
    """Wire contract the client relies on: clock_tick.payload.paused flips."""
    bus = hve._bus
    q = await bus.subscribe()

    async def drain_kind(kind: str):
        # Drain queue and return the most recent event of `kind`, or None.
        latest = None
        while not q.empty():
            evt = q.get_nowait()
            if evt.kind == kind:
                latest = evt
        return latest

    await hve.new_game(human_white=True, tc=TimeControl(30.0, 0.0))
    tick = await drain_kind("clock_tick")
    assert tick is not None
    assert tick.payload["paused"] is False
    assert tick.payload["running"] is True

    await hve.pause()
    tick = await drain_kind("clock_tick")
    assert tick is not None
    assert tick.payload["paused"] is True
    assert tick.payload["running"] is False

    await hve.resume()
    tick = await drain_kind("clock_tick")
    assert tick is not None
    assert tick.payload["paused"] is False
    assert tick.payload["running"] is True

    await bus.unsubscribe(q)
