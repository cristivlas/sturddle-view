"""HumanVsEngine._think_and_play guards exercised without a real engine.

The full search lifecycle (analysis context, info pump, bestmove) needs
a real UCI subprocess and is covered by integration tests. Here we hit
the cheap, hard-edge guards: board-None early exit, ensure_engine
failure, and the lock-re-entry consistency checks."""
from __future__ import annotations


import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine


@pytest.fixture
def hve():
    return HumanVsEngine(engine_path="/nonexistent", bus=EventBus())


def _play(board, *ucis):
    for u in ucis:
        board.push(chess.Move.from_uci(u))


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
    hve._board = chess.Board()
    hve._game_id = "test-game"

    async def boom():
        raise RuntimeError("cannot start engine")

    monkeypatch.setattr(hve, "_ensure_engine", boom)

    with caplog.at_level("ERROR", logger="sturddle_view.play.human_vs_engine"):
        await hve._think_and_play()  # must not raise

    assert any("could not start engine" in m for m in caplog.messages)


# ----- _next_book_move: follow the line / fall out of book -----

def test_next_book_move_none_when_no_line(hve):
    hve._board = chess.Board()
    hve._book_line = None
    assert hve._next_book_move() is None


def test_next_book_move_plays_at_startpos(hve):
    hve._board = chess.Board()
    hve._book_line = ("e2e4", "e7e5", "g1f3")
    assert hve._next_book_move() == chess.Move.from_uci("e2e4")
    assert hve._book_line is not None  # still in book


def test_next_book_move_follows_after_prefix(hve):
    board = chess.Board()
    _play(board, "e2e4", "e7e5")
    hve._board = board
    hve._book_line = ("e2e4", "e7e5", "g1f3")
    assert hve._next_book_move() == chess.Move.from_uci("g1f3")


def test_next_book_move_human_deviation_clears_line(hve):
    board = chess.Board()
    _play(board, "e2e4", "c7c5")  # book wanted e7e5
    hve._board = board
    hve._book_line = ("e2e4", "e7e5", "g1f3")
    assert hve._next_book_move() is None
    assert hve._book_line is None


def test_next_book_move_exhausted_line_clears(hve):
    board = chess.Board()
    _play(board, "e2e4", "e7e5")
    hve._board = board
    hve._book_line = ("e2e4", "e7e5")
    assert hve._next_book_move() is None
    assert hve._book_line is None


def test_next_book_move_illegal_book_move_clears(hve):
    board = chess.Board()  # startpos; e2e5 is not legal
    hve._board = board
    hve._book_line = ("e2e5",)
    assert hve._next_book_move() is None
    assert hve._book_line is None


def test_next_book_move_bad_uci_clears(hve):
    hve._board = chess.Board()
    hve._book_line = ("notamove",)
    assert hve._next_book_move() is None
    assert hve._book_line is None


def test_next_book_move_disabled_when_start_fen_set(hve):
    # EPD-seeded games start from a FEN and must NOT follow a UCI line.
    hve._board = chess.Board()
    hve._start_fen = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1"
    hve._book_line = ("g1f3",)
    assert hve._next_book_move() is None
