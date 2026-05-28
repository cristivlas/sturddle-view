"""swap_engine must not leak the previous engine's UCI options/args/env.

The API layer re-seeds these on every fetch via the `set_engine_*` setters,
but a swap initiated outside that fetch loop (or before the next fetch)
should not apply the prior binary's per-engine config to a different binary.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl
from sturddle_view.play.mode import Mode


class _StubEngine:
    def __init__(self) -> None:
        self.quit_called = False

    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        self.quit_called = True


@pytest.fixture
def hve():
    bus = EventBus()
    h = HumanVsEngine(engine_path="/engine_a", bus=bus)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


async def test_swap_engine_clears_per_engine_options(hve):
    """Per-engine UCI options stored for engine A must not carry over to
    engine B. Engine B's options are re-seeded by the API layer on next
    fetch; until then, the registry has the right answer (empty)."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    hve.set_engine_options({"Hash": 512, "EvalFile": "engine_a.nnue"})

    await hve.swap_engine("/engine_b")

    assert hve._engine_options == {}


async def test_swap_engine_clears_per_engine_args(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    hve.set_engine_args(["--profile", "engine_a"])

    await hve.swap_engine("/engine_b")

    assert hve._engine_args == []


async def test_swap_engine_clears_per_engine_env(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    hve.set_engine_env({"ENGINE_A_FLAG": "1"})

    await hve.swap_engine("/engine_b")

    assert hve._engine_env == {}


# -------- engine-kick gate: only in live play --------


async def test_swap_engine_does_not_kick_when_viewing(hve):
    """swap_engine has no Op-level guard. In VIEWING there is no live game,
    so the engine must NOT be kicked after the swap."""
    await hve.new_game(human_white=False, tc=TimeControl(60.0, 0.0))
    hve._engine_to_move.reset_mock()
    hve._mode = Mode.VIEWING
    await hve.swap_engine("/engine_b")
    hve._engine_to_move.assert_not_awaited()


async def test_swap_engine_does_not_kick_when_editing(hve):
    await hve.new_game(human_white=False, tc=TimeControl(60.0, 0.0))
    hve._engine_to_move.reset_mock()
    hve._mode = Mode.EDITING
    await hve.swap_engine("/engine_b")
    hve._engine_to_move.assert_not_awaited()


async def test_swap_engine_kicks_in_play_when_engine_to_move(hve):
    """Sanity: PLAY mode + engine to move still kicks after a swap."""
    await hve.new_game(human_white=False, tc=TimeControl(60.0, 0.0))
    hve._engine_to_move.reset_mock()
    assert hve._mode is Mode.PLAY
    await hve.swap_engine("/engine_b")
    hve._engine_to_move.assert_awaited_once()
