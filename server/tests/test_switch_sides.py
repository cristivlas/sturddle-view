"""switch_sides engine-kick gate.

SWITCH_SIDES is allowed by the FSM in PLAY, PAUSED, ANALYZING, and VIEWING --
the latter two flip board orientation only and must NOT trigger an engine
search. Drives HumanVsEngine without a real UCI engine.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl
from sturddle_view.play.mode import Mode


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


async def test_switch_sides_kicks_in_play_when_engine_to_move(hve):
    """Human is white, engine plays black. After switch, human is black --
    engine (now white) is to move from the start position, so kick."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    hve._engine_to_move.reset_mock()
    assert hve._mode is Mode.PLAY
    await hve.switch_sides()
    hve._engine_to_move.assert_awaited_once()


async def test_switch_sides_does_not_kick_when_paused(hve):
    """Resume(), not switch_sides, owns the engine kick after a pause."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.pause()
    hve._engine_to_move.reset_mock()
    await hve.switch_sides()
    hve._engine_to_move.assert_not_awaited()


async def test_switch_sides_does_not_kick_when_analyzing(hve):
    """Analysis runs its own engine session; switch_sides must not start a
    competing _think_task. Set the mode directly to avoid spawning the real
    analysis task (which would need a UCI engine)."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    hve._engine_to_move.reset_mock()
    hve._pre_analysis_mode = Mode.PAUSED
    hve._mode = Mode.ANALYZING
    await hve.switch_sides()
    hve._engine_to_move.assert_not_awaited()


async def test_switch_sides_does_not_kick_when_viewing(hve):
    """VIEWING has no live game -- switch_sides flips orientation only."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    hve._engine_to_move.reset_mock()
    hve._mode = Mode.VIEWING
    await hve.switch_sides()
    hve._engine_to_move.assert_not_awaited()
