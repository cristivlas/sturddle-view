"""HumanVsEngine.new_game validation + auto-engine-move semantics.

Covers the seed-FEN/UCI error paths and the engine-to-move kickoff that
fires when the seeded position has the engine on move."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve(tmp_path):
    settings = Settings()
    settings.pgn_dir = tmp_path
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, settings=settings)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


_TC = TimeControl(initial_seconds=60.0, increment_seconds=0.0)


async def test_new_game_invalid_fen_raises_runtime_error(hve):
    """Bad start_fen → ValueError from board_from → wrapped as
    RuntimeError('invalid FEN'). Kills ExceptionReplacer mutations on
    the `except ValueError` catch (replacement classes would let the
    ValueError propagate instead of producing the wrapped RuntimeError)."""
    with pytest.raises(RuntimeError, match="invalid FEN"):
        await hve.new_game(human_white=True, tc=_TC, start_fen="not a fen")


async def test_new_game_invalid_uci_in_seed_moves_raises_runtime_error(hve):
    """Malformed UCI in start_moves_uci → ValueError from chess.Move.from_uci
    → wrapped as RuntimeError('invalid UCI in seed moves: ...'). Kills
    ExceptionReplacer on the second `except ValueError` catch."""
    with pytest.raises(RuntimeError, match="invalid UCI in seed moves: zzz"):
        await hve.new_game(
            human_white=True, tc=_TC,
            start_moves_uci=["zzz"],
        )


async def test_new_game_illegal_seed_move_raises_runtime_error(hve):
    """A well-formed but illegal UCI move (e.g. e2e6 from startpos) raises
    'illegal seed move'. Distinct from the invalid-UCI path; ensures the
    legality check fires before pushing."""
    with pytest.raises(RuntimeError, match="illegal seed move: e2e6"):
        await hve.new_game(
            human_white=True, tc=_TC,
            start_moves_uci=["e2e6"],
        )


async def test_new_game_seeded_position_already_over_raises(hve):
    """Seeded position is terminal → raises. Pins the
    `if board.is_game_over():` guard from being short-circuited."""
    # Fool's mate position (1. f3 e5 2. g4 Qh4#).
    with pytest.raises(RuntimeError, match="already over"):
        await hve.new_game(
            human_white=True, tc=_TC,
            start_moves_uci=["f2f3", "e7e5", "g2g4", "d8h4"],
        )


async def test_new_game_human_white_does_not_kick_engine(hve):
    """human_white=True + startpos → white-to-move is human → no auto
    engine kick. Kills AddNot / Eq mutations on the
    `if self._board.turn == self._engine_color():` engine-trigger guard."""
    await hve.new_game(human_white=True, tc=_TC)
    hve._engine_to_move.assert_not_awaited()


async def test_new_game_human_black_kicks_engine_immediately(hve):
    """human_white=False + startpos → white-to-move is engine → auto
    engine kick must fire once. Pins the same engine-trigger guard from
    the True direction."""
    await hve.new_game(human_white=False, tc=_TC)
    hve._engine_to_move.assert_awaited_once()


async def test_new_game_seeded_with_black_to_move_human_black_does_not_kick(hve):
    """Custom FEN with black to move + human_white=False → black is human →
    no engine kick. Pins the engine-trigger guard against a non-startpos seed."""
    # Black-to-move position after 1.e4.
    fen_black_to_move = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    await hve.new_game(
        human_white=False, tc=_TC,
        start_fen=fen_black_to_move,
    )
    hve._engine_to_move.assert_not_awaited()


async def test_new_game_seeded_with_black_to_move_human_white_kicks_engine(hve):
    """Custom FEN with black to move + human_white=True → engine plays
    black → auto kick must fire. Mirror of the previous test; together
    they pin both directions of the turn==engine_color() comparison."""
    fen_black_to_move = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    await hve.new_game(
        human_white=True, tc=_TC,
        start_fen=fen_black_to_move,
    )
    hve._engine_to_move.assert_awaited_once()
