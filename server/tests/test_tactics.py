"""Pins and forks core (play/tactics.py): representative positions.

Pins: absolute (to the king), relative to the queen, blocked rays, a check
is not a pin. Forks: knight, pawn, king; defended minors excluded, majors
counted regardless.
"""
from __future__ import annotations

import chess

from sturddle_view.play.tactics import (
    Fork,
    Pin,
    all_forks,
    all_pins,
    forks,
    piece_label,
    pins,
)


def _sq(name: str) -> int:
    return chess.parse_square(name)


# Ruy Lopez after 3...d6: Bb5 pins the c6 knight to the e8 king.
_RUY_PIN_FEN = "r1bqkbnr/ppp2ppp/2np4/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4"
# Re1 pins the e5 knight to the e8 queen (relative pin). Ra1 sees the a8
# king first, which is a check, not a pin.
_QUEEN_PIN_FEN = "k3q3/8/8/4n3/8/8/8/K3R3 w - - 0 1"
# Same, with a white pawn on e3 blocking the rook's ray.
_BLOCKED_RAY_FEN = "k3q3/8/8/4n3/8/4P3/8/K3R3 w - - 0 1"
# Nc7+ forks the a8 rook and the e8 king.
_KNIGHT_FORK_FEN = "r3k3/2N5/8/8/8/8/8/4K3 b - - 0 1"
# d5 pawn attacks an undefended knight (c6) and bishop (e6).
_PAWN_FORK_FEN = "k7/8/2n1b3/3P4/8/8/8/K7 w - - 0 1"
# Same, with b7 defending the knight: only one target left.
_PAWN_NO_FORK_FEN = "k7/1p6/2n1b3/3P4/8/8/8/K7 w - - 0 1"
# Nd4 attacks two rooks that defend each other: majors count anyway.
_ROOK_FORK_FEN = "k7/8/2r1r3/8/3N4/8/8/K7 w - - 0 1"
# Kd4 attacks two undefended pawns.
_KING_FORK_FEN = "k7/8/8/2p1p3/3K4/8/8/8 w - - 0 1"
# Kd4 attacks two pawns the d6 king defends: a king takes only free pieces.
_KING_NO_FORK_FEN = "8/8/3k4/2p1p3/3K4/8/8/8 w - - 0 1"


def test_startpos_has_no_tactics():
    board = chess.Board()
    assert all_pins(board) == []
    assert all_forks(board) == []


def test_absolute_pin_to_king():
    board = chess.Board(_RUY_PIN_FEN)
    assert pins(board, chess.BLACK) == [
        Pin(chess.BLACK, _sq("b5"), _sq("c6"), _sq("e8")),
    ]
    assert pins(board, chess.WHITE) == []


def test_relative_pin_to_queen_and_check_is_not_a_pin():
    board = chess.Board(_QUEEN_PIN_FEN)
    assert pins(board, chess.BLACK) == [
        Pin(chess.BLACK, _sq("e1"), _sq("e5"), _sq("e8")),
    ]


def test_blocked_ray_is_not_a_pin():
    assert all_pins(chess.Board(_BLOCKED_RAY_FEN)) == []


def test_knight_fork_on_king_and_rook():
    board = chess.Board(_KNIGHT_FORK_FEN)
    assert forks(board, chess.WHITE) == [
        Fork(chess.WHITE, _sq("c7"), (_sq("a8"), _sq("e8"))),
    ]
    assert forks(board, chess.BLACK) == []


def test_pawn_fork_on_undefended_minors():
    board = chess.Board(_PAWN_FORK_FEN)
    assert forks(board, chess.WHITE) == [
        Fork(chess.WHITE, _sq("d5"), (_sq("c6"), _sq("e6"))),
    ]


def test_defended_minor_is_not_a_fork_target():
    assert forks(chess.Board(_PAWN_NO_FORK_FEN), chess.WHITE) == []


def test_defended_majors_still_count_as_fork_targets():
    board = chess.Board(_ROOK_FORK_FEN)
    assert forks(board, chess.WHITE) == [
        Fork(chess.WHITE, _sq("d4"), (_sq("c6"), _sq("e6"))),
    ]


def test_king_forks_only_undefended_pieces():
    assert forks(chess.Board(_KING_FORK_FEN), chess.WHITE) == [
        Fork(chess.WHITE, _sq("d4"), (_sq("c5"), _sq("e5"))),
    ]
    assert forks(chess.Board(_KING_NO_FORK_FEN), chess.WHITE) == []


def test_piece_label_names_color_type_and_square():
    board = chess.Board(_RUY_PIN_FEN)
    assert piece_label(board, _sq("c6")) == "black knight on c6"
    assert piece_label(board, _sq("b5")) == "white bishop on b5"
