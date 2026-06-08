"""Take-back behavior, focused on clock restoration.

Tests drive HumanVsEngine without spawning a real UCI engine. We override
`_ensure_engine` to install a stub and `_engine_to_move` to be a no-op so
we can inject the engine's "reply" by calling internal methods deterministically.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl


class _StubEngine:
    """Minimal stand-in for chess.engine.UciProtocol."""
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve(monkeypatch):
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine

    # Suppress automatic engine moves; tests inject them explicitly.
    h._engine_to_move = AsyncMock()
    return h


async def _engine_reply(h: HumanVsEngine, uci: str, score: dict | None = None) -> None:
    """Mimic the post-search portion of _think_and_play for one move.

    Real `_think_and_play` snapshots clocks, consumes time, pushes the move,
    appends to eval_history, and publishes. We do exactly that.
    """
    move = chess.Move.from_uci(uci)
    async with h._lock:
        h._clock.history.append((h._clock.white_time, h._clock.black_time))
        h._consume_turn_time()
        h._board.push(move)
        h._eval_history.append(score)
        await h._publish_board()
        await h._publish_clock()


async def test_takeback_restores_both_clocks_after_engine_reply(hve):
    # Human is white, 60s base, 0 inc, no real engine.
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))

    # Spend ~0.05s on white's move, then submit.
    await asyncio.sleep(0.05)
    await hve.submit_move("e2e4")
    white_after_human = hve._clock.white_time
    black_after_human = hve._clock.black_time
    assert white_after_human < 60.0  # human consumed time
    assert black_after_human == pytest.approx(60.0, abs=1e-6)

    # Engine "thinks" 0.05s and replies.
    await asyncio.sleep(0.05)
    await _engine_reply(hve, "e7e5")
    black_after_engine = hve._clock.black_time
    assert black_after_engine < 60.0  # engine consumed time

    # Take-back: should restore to BEFORE human played e2e4.
    await hve.takeback()
    assert hve._clock.white_time == pytest.approx(60.0, abs=1e-6)
    assert hve._clock.black_time == pytest.approx(60.0, abs=1e-6)
    # Move stack and history are empty again.
    assert hve._board.move_stack == []
    assert hve._clock.history == []


async def test_takeback_after_only_human_move_restores(hve):
    # Engine is "thinking" (we suppressed _engine_to_move). Take-back removes
    # the human's just-played move and restores the human's clock.
    await hve.new_game(human_white=True, tc=TimeControl(30.0, 0.0))
    await asyncio.sleep(0.05)
    await hve.submit_move("d2d4")
    assert hve._clock.white_time < 30.0

    await hve.takeback()
    assert hve._clock.white_time == pytest.approx(30.0, abs=1e-6)
    assert hve._clock.black_time == pytest.approx(30.0, abs=1e-6)
    assert hve._board.move_stack == []


async def test_takeback_with_increment_does_not_double_credit(hve):
    # 30s base, 5s increment. Submitting a move adds 5s to the human's clock;
    # take-back must un-add the increment along with restoring spent time.
    await hve.new_game(human_white=True, tc=TimeControl(30.0, 5.0))
    await asyncio.sleep(0.05)
    await hve.submit_move("e2e4")
    # After consume_turn_time: white_time = max(0, 30 - 0.05) + 5 ≈ 34.95
    assert hve._clock.white_time > 30.0  # increment applied

    await asyncio.sleep(0.05)
    await _engine_reply(hve, "e7e5")
    await hve.takeback()
    # Both clocks back to the pristine 30.0.
    assert hve._clock.white_time == pytest.approx(30.0, abs=1e-6)
    assert hve._clock.black_time == pytest.approx(30.0, abs=1e-6)


async def test_takeback_with_no_moves_raises(hve):
    await hve.new_game(human_white=True, tc=TimeControl(30.0, 0.0))
    with pytest.raises(RuntimeError, match="nothing to take back"):
        await hve.takeback()


async def test_takeback_while_paused_stays_paused(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    await _engine_reply(hve, "e7e5")
    await hve.pause()
    assert hve.is_paused
    await hve.takeback()
    assert hve.is_paused
    assert hve._board.move_stack == []


async def test_takeback_after_pgn_seeded_game(hve):
    # Regression: seeding plies via start_moves_uci must populate _clock_history
    # so takeback's pop() doesn't IndexError. Reproduces the import-PGN-then-undo
    # crash with seed = 1.e4 c5 2.Nf3 (3 plies, white to move == human's turn).
    await hve.new_game(
        human_white=True,
        tc=TimeControl(60.0, 0.0),
        start_moves_uci=["e2e4", "c7c5", "g1f3"],
    )
    assert len(hve._board.move_stack) == 3
    assert len(hve._clock.history) == 3  # invariant: one per ply

    await hve.takeback()
    # Black to move after Nf3 → engine-thinking branch: pop just the last ply.
    assert [m.uci() for m in hve._board.move_stack] == ["e2e4", "c7c5"]
    assert hve._clock.white_time == pytest.approx(60.0, abs=1e-6)
    assert hve._clock.black_time == pytest.approx(60.0, abs=1e-6)
