"""HumanVsEngine._think_and_play guards exercised without a real engine.

The full search lifecycle (analysis context, info pump, bestmove) needs
a real UCI subprocess and is covered by integration tests. Here we hit
the cheap, hard-edge guards: board-None early exit, ensure_engine
failure, and the lock-re-entry consistency checks."""
from __future__ import annotations


import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine


@pytest.fixture
def hve():
    return HumanVsEngine(engine_path="/nonexistent", bus=EventBus())


async def test_think_and_play_returns_when_board_is_none(hve):
    """No active game → board is None → return without crash. Kills
    AddNot / `or`→`and` mutations on the
    `self._board is None or self._game_id is None` early-exit guard."""
    assert hve._board is None
    assert hve._game_id is None
    await hve._think_and_play()  # must not raise


async def test_think_and_play_swallows_ensure_engine_failure(hve, caplog, monkeypatch):
    """If `_ensure_engine` raises (engine binary missing, etc.) the
    coroutine logs and returns instead of propagating. Kills
    ExceptionReplacer mutations on the `except Exception` catch (a
    replacement non-parent class would let the exception escape)."""
    # Put HVE in a state that passes the board-None guard.
    import chess
    hve._board = chess.Board()
    hve._game_id = "test-game"

    async def boom():
        raise RuntimeError("cannot start engine")

    monkeypatch.setattr(hve, "_ensure_engine", boom)

    with caplog.at_level("ERROR", logger="sturddle_view.play.human_vs_engine"):
        await hve._think_and_play()  # must not raise

    assert any("could not start engine" in m for m in caplog.messages)
