"""Unit tests for chess/board.py helpers (R1).

These tests are RED until chess/board.py is created (Commit C).
"""
from __future__ import annotations

import pytest
import chess

from sturddle_view.chess.board import (
    board_from,
    moves_san,
    replay_uci,
    side_to_move,
)

VALID_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"
INVALID_FEN = "not/a/fen"
BLACK_TO_MOVE_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"


def test_board_from_none_is_startpos():
    b = board_from(None)
    assert b.fen() == chess.STARTING_FEN


def test_board_from_fen_round_trips():
    b = board_from(VALID_FEN)
    assert b.fen() == VALID_FEN


def test_board_from_invalid_fen_raises_valueerror():
    with pytest.raises(ValueError):
        board_from(INVALID_FEN)


def test_replay_uci_from_startpos():
    b = board_from(None)
    b2 = replay_uci(b, ["e2e4", "e7e5"])
    assert len(b2.move_stack) == 2
    assert b2.peek().uci() == "e7e5"


def test_replay_uci_from_custom_fen():
    b = board_from(VALID_FEN)
    b2 = replay_uci(b, ["g1f3"])
    assert len(b2.move_stack) == 1


def test_replay_uci_rejects_illegal_move():
    b = board_from(None)
    with pytest.raises(ValueError):
        replay_uci(b, ["e2e5"])  # illegal


def test_replay_uci_rejects_malformed_uci():
    b = board_from(None)
    with pytest.raises(ValueError):
        replay_uci(b, ["notauci"])


def test_side_to_move_white_black():
    assert side_to_move(board_from(None)) == "white"
    assert side_to_move(board_from(BLACK_TO_MOVE_FEN)) == "black"


def test_moves_san_from_startpos():
    b = board_from(None)
    b.push_uci("e2e4")
    b.push_uci("e7e5")
    result = moves_san(b, None)
    assert result == ["e4", "e5"]


def test_moves_san_from_custom_fen():
    b = board_from(VALID_FEN)
    b.push_uci("g1f3")
    result = moves_san(b, VALID_FEN)
    assert result == ["Nf3"]
