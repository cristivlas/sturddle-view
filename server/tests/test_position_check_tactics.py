"""Pin/fork prose check (position_check.iter_false_tactic_claims).

A tactic word plus a named piece or square is held against the pins and
forks on the board (or one SAN hop away). Hedged clauses are skipped.
"""
from __future__ import annotations

import chess

from sturddle_view.llm.position_check import (
    describe_tactics,
    find_false_tactic_claims,
    has_position_flags,
    iter_false_tactic_claims,
)


# Bb5 pins the c6 knight to the e8 king; no fork on the board.
_RUY_PIN_FEN = "r1bqkbnr/ppp2ppp/2np4/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4"
# One move earlier: Bb5 is the move to come.
_RUY_PRE_PIN_FEN = "r1bqkbnr/ppp2ppp/2np4/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 0 4"
# Nc7+ forks the a8 rook and the e8 king.
_KNIGHT_FORK_FEN = "r3k3/2N5/8/8/8/8/8/4K3 b - - 0 1"


def test_true_pin_claim_is_not_flagged():
    board = chess.Board(_RUY_PIN_FEN)
    for text in (
        "The knight on c6 is pinned to the king.",
        "The bishop on b5 pins the knight.",
        "Black's pinned knight cannot move.",
        "The pin on c6 is annoying.",
    ):
        assert find_false_tactic_claims(text, board) == [], text


def test_false_pin_claim_is_flagged_with_clause_surface_and_fact():
    board = chess.Board(_RUY_PIN_FEN)
    text = "White stands well. The knight on f3 is pinned, which hurts."
    flags = list(iter_false_tactic_claims(text, board))
    assert len(flags) == 1
    surface, label, fact = flags[0]
    assert surface == "The knight on f3 is pinned"
    assert label == "pin: knight, f3"
    assert fact == describe_tactics(board, "pin")
    assert "black knight on c6 pinned to black king on e8 by white bishop on b5" in fact


def test_pin_claim_with_wrong_piece_word_is_flagged():
    board = chess.Board(_RUY_PIN_FEN)
    assert find_false_tactic_claims("The rook is pinned.", board) == ["pin: rook"]


def test_fork_claims():
    board = chess.Board(_KNIGHT_FORK_FEN)
    assert find_false_tactic_claims("The knight forks the king and rook.", board) == []
    assert find_false_tactic_claims("Nc7 forks king and rook.", board) == []
    assert find_false_tactic_claims("The knight forks the king and queen.", board) == [
        "fork: king, knight, queen",
    ]


def test_no_fork_on_board_gives_no_forks_fact():
    board = chess.Board(_RUY_PIN_FEN)
    flags = list(iter_false_tactic_claims("The bishop forks the queen and rook.", board))
    assert [f for _s, _l, f in flags] == ["no forks on the board"]


def test_claim_projected_one_move_ahead_clears():
    board = chess.Board(_RUY_PRE_PIN_FEN)
    assert find_false_tactic_claims("Bb5 pins the knight on c6.", board) == []
    # Without the SAN hop the same clause is false on this board.
    assert find_false_tactic_claims("The knight on c6 is pinned.", board) == ["pin: knight, c6"]


def test_hedged_clauses_are_skipped():
    board = chess.Board(_RUY_PRE_PIN_FEN)
    for text in (
        "Bb5 would pin the knight on c6.",
        "The knight on c6 is not pinned.",
        "White threatens to pin the knight.",
        "After Bb5 the knight is pinned.",
        "The knight isn't pinned.",
        "Black can unpin the knight with ...Bd7.",
    ):
        assert find_false_tactic_claims(text, board) == [], text


def test_clause_without_piece_or_square_is_ungroundable():
    board = chess.Board(_RUY_PRE_PIN_FEN)
    assert find_false_tactic_claims("There is a nasty pin here.", board) == []


def test_repeated_claim_is_flagged_once():
    board = chess.Board(_RUY_PIN_FEN)
    text = "The knight on f3 is pinned. Again, the knight on f3 is pinned."
    assert find_false_tactic_claims(text, board) == ["pin: knight, f3"]


def test_clause_bounds_do_not_split_on_move_number_dots():
    board = chess.Board(_RUY_PIN_FEN)
    # "4.Bb5" keeps its clause: the pin claim names the c6 knight and holds.
    assert find_false_tactic_claims("4.Bb5 pins the knight on c6 for good.", board) == []


def test_has_position_flags_sees_tactic_claims():
    before = chess.Board(_RUY_PIN_FEN)
    after = before.copy()
    after.push_san("O-O")
    assert has_position_flags("The knight on f3 is pinned.", before, after)
    assert not has_position_flags("The knight on c6 is pinned.", before, after)
