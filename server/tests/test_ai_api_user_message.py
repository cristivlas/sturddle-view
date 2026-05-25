"""_ai_kick._build_user_message: builds the initial user message from
live hve state.

It reaches into `hve._board` and `hve._start_fen` -- same coupling
shortcut that api/game.py uses for `game_id`. When the coordinator
owns its own session state, this test moves with the seam.
"""
from __future__ import annotations

import chess

from sturddle_view.api._ai_kick import _build_user_message


class _FakeHve:
    """Minimal stand-in: only the three attributes _build_user_message reads."""
    def __init__(self, board: chess.Board | None, start_fen: str | None = None):
        self._board = board
        self._start_fen = start_fen


def test_build_user_message_handles_no_hve():
    assert _build_user_message(None) is None


def test_build_user_message_handles_no_board():
    assert _build_user_message(_FakeHve(board=None)) is None


def test_build_user_message_startpos_no_moves():
    board = chess.Board()
    msg = _build_user_message(_FakeHve(board=board))
    assert msg is not None
    assert board.fen() in msg
    assert "(none yet" in msg


def test_build_user_message_after_moves():
    board = chess.Board()
    board.push_san("e4")
    board.push_san("e5")
    board.push_san("Nf3")

    msg = _build_user_message(_FakeHve(board=board))
    assert msg is not None
    assert board.fen() in msg
    assert "1. e4 e5 2. Nf3" in msg


def test_build_user_message_honors_start_fen():
    # Imported game whose move_stack is replayed from a custom start_fen.
    # Without honoring _start_fen, moves_san would replay from startpos
    # and either crash or produce wrong SAN.
    start_fen = "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"
    board = chess.Board(start_fen)
    board.push_san("e4")

    msg = _build_user_message(_FakeHve(board=board, start_fen=start_fen))
    assert msg is not None
    assert "1. e4" in msg
    assert board.fen() in msg
