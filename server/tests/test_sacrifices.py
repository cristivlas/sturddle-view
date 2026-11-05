"""Speculative sacrifices: the pure board detector (play/sacrifices.py,
docs/ai-playbook-spec.md, Speculative sacrifices). Each pattern has a
position where it fires and near-misses that drop one precondition.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.play.sacrifices import (
    PATTERN_BISHOP_F7,
    PATTERN_GREEK_GIFT,
    sacrifice_moves,
    static_exchange,
)


# French: Bd3, Nf3, Qd1 against a king on g8, nothing covering g5.
_GREEK_GIFT = "r1b2rk1/ppqn1ppp/2n1p3/2bpP3/8/2PB1N2/PP1N1PPP/R1BQ1RK1 w - - 0 1"
# Italian: Bc4 against f7, the king still on e8.
_ITALIAN = "r1bqk1nr/pppp1ppp/2n5/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"


def _moves(fen: str) -> tuple[tuple[str, str], ...]:
    return sacrifice_moves(chess.Board(fen))


def test_greek_gift_fires():
    assert _moves(_GREEK_GIFT) == ((PATTERN_GREEK_GIFT, "Bxh7+"),)


def test_greek_gift_fires_for_black():
    board = chess.Board(_GREEK_GIFT).mirror()
    assert sacrifice_moves(board) == ((PATTERN_GREEK_GIFT, "Bxh2+"),)


@pytest.mark.parametrize("fen", [
    # No knight to reach g5.
    "r1b2rk1/ppqn1ppp/2n1p3/2bpP3/8/2PB4/PP1N1PPP/R1BQ1RK1 w - - 0 1",
    # No queen to reach the h-file.
    "r1b2rk1/ppqn1ppp/2n1p3/2bpP3/8/2PB1N2/PP1N1PPP/R1B2RK1 w - - 0 1",
    # The queen reaches only h1, behind the h2 pawn.
    "r1b2rk1/ppqn1ppp/2n1p3/2bpP3/8/2PB1N2/PP1NP1PP/R1KQ4 w - - 0 1",
    # King on h8, not g8.
    "r1b2r1k/ppqn1ppp/2n1p3/2bpP3/8/2PB1N2/PP1N1PPP/R1BQ1RK1 w - - 0 1",
    # A pawn on f6 covers g5.
    "r1b2rk1/ppqn2pp/2n1pp2/2bpP3/8/2PB1N2/PP1N1PPP/R1BQ1RK1 w - - 0 1",
    # A bishop on e7 wins the knight on g5.
    "r1b2rk1/ppqnbppp/2n1p3/3pP3/8/2PB1N2/PP1N1PPP/R1BQ1RK1 w - - 0 1",
    # A knight on f6 also guards h7: ...Nxh7 wins the bishop.
    "r1b2rk1/ppq2ppp/2n1pn2/2bpP3/8/2PB1N2/PP1N1PPP/R1BQ1RK1 w - - 0 1",
    # Qh5 defends h7: Bxh7+ wins a pawn, it is no sacrifice.
    "r1b2rk1/ppqn1ppp/2n1p3/2bpP2Q/8/2PB1N2/PP1N1PPP/R1B2RK1 w - - 0 1",
])
def test_greek_gift_near_misses(fen):
    assert all(pattern != PATTERN_GREEK_GIFT for pattern, _ in _moves(fen))


def test_bishop_f7_fires():
    assert _moves(_ITALIAN) == ((PATTERN_BISHOP_F7, "Bxf7+"),)


def test_bishop_f7_fires_for_black():
    board = chess.Board(_ITALIAN).mirror()
    assert sacrifice_moves(board) == ((PATTERN_BISHOP_F7, "Bxf2+"),)


@pytest.mark.parametrize("fen", [
    # The king has castled.
    "r1bq1rk1/pppp1ppp/2n2n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 w - - 6 5",
    # Ng5 also hits f7: Bxf7+ wins a pawn, it is no sacrifice.
    "r1bqk2r/pppp1ppp/2n2n2/2b1p1N1/2B1P3/8/PPPP1PPP/RNBQK2R w KQkq - 6 5",
])
def test_bishop_f7_near_misses(fen):
    assert all(pattern != PATTERN_BISHOP_F7 for pattern, _ in _moves(fen))


def test_start_position_has_none():
    assert sacrifice_moves(chess.Board()) == ()


@pytest.mark.parametrize("fen, uci, expected", [
    # Bxh7+ Kxh7: a bishop for a pawn.
    (_GREEK_GIFT, "d3h7", -200),
    # Qh5 keeps the king off h7: the pawn is free.
    ("r1b2rk1/ppqn1ppp/2n1p3/2bpP2Q/8/2PB1N2/PP1N1PPP/R1B2RK1 w - - 0 1", "d3h7", 100),
    # exd5 exd5: a pawn trade, even.
    ("rnbqkbnr/ppp2ppp/4p3/3p4/3PP3/8/PPP2PPP/RNBQKBNR w KQkq - 0 3", "e4d5", 0),
])
def test_static_exchange(fen, uci, expected):
    board = chess.Board(fen)
    assert static_exchange(board, chess.Move.from_uci(uci)) == expected
