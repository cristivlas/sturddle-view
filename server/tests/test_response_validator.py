"""Move-legality validator that scans model prose for SAN-shaped tokens.

Lives in `llm/response_validator.py`. Pure function over (text, board).
These tests pin behavior at the API level; the regex is implementation
detail and may evolve as we learn what real model output produces.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.llm.response_validator import (
    find_castle_word_violations,
    find_false_piece_claims,
    find_illegal_moves,
)


def test_empty_text_returns_empty():
    assert find_illegal_moves("", chess.Board()) == []


def test_no_move_tokens_returns_empty():
    text = "The position is balanced, both sides have chances."
    assert find_illegal_moves(text, chess.Board()) == []




def test_legal_piece_move_passes():
    assert find_illegal_moves("Develop with Nf3.", chess.Board()) == []


def test_bare_pawn_move_not_flagged():
    # Bare-square pawn moves (e4, e5, h6, ...) are out of scope: chess
    # prose mentions squares as description constantly. Loss accepted.
    assert find_illegal_moves("White plays e5.", chess.Board()) == []
    assert find_illegal_moves("The h6 pawn is weak.", chess.Board()) == []


def test_illegal_piece_move_detected():
    # Nf6 is on a black square; the white knight from g1 can't reach it.
    assert find_illegal_moves("White plays Nf6.", chess.Board()) == ["Nf6"]


def test_duplicate_illegal_move_listed_once():
    text = "Try Nf6. No really, Nf6 is the move."
    assert find_illegal_moves(text, chess.Board()) == ["Nf6"]


def test_annotation_glyphs_preserved_in_token():
    # The token includes the glyph so the corrective message echoes
    # what the model wrote.
    assert find_illegal_moves("White plays Nf6!?.", chess.Board()) == ["Nf6!?"]


def test_mixed_legal_and_illegal():
    text = "Nf3 is fine, but Nf6 is wrong, and e4 works."
    assert find_illegal_moves(text, chess.Board()) == ["Nf6"]


def test_invalid_san_ignored_not_flagged():
    # "Xyz" doesn't match the regex; "Nzz" matches piece-prefix but
    # parse_san raises InvalidMoveError, not IllegalMoveError.
    text = "The position is complex; consider Xyz or other ideas."
    assert find_illegal_moves(text, chess.Board()) == []


def test_castling_legal_in_appropriate_position():
    board = chess.Board("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")
    assert find_illegal_moves("White can play O-O.", board) == []


def test_castling_illegal_when_blocked():
    # Starting position: castling is illegal (pieces in the way).
    assert find_illegal_moves("White plays O-O.", chess.Board()) == ["O-O"]


@pytest.mark.parametrize("token", ["Nf3", "Nc3"])
def test_legal_piece_first_moves_pass(token):
    assert find_illegal_moves(f"Consider {token}.", chess.Board()) == []


def test_illegal_pawn_capture_detected():
    # Pawn captures keep the file prefix (`exd5`), so they are
    # syntactically distinct from bare-square prose and remain in scope.
    assert find_illegal_moves("White plays exd5.", chess.Board()) == ["exd5"]


def test_illegal_queen_move_in_prose_detected():
    # Regression: model called validate_move(Qg4) -> illegal, then wrote
    # prose recommending "Qg4" anyway. Round-end validator must flag it.
    text = (
        "A strong candidate is Qg4, which develops the queen with tempo "
        "and eyes at f7."
    )
    assert find_illegal_moves(text, chess.Board()) == ["Qg4"]


# ---------- Piece-on-square claims ------------------------------------

# Real FEN captured from a qwen3:30b hallucination logged in
# ai-transcript.log. Used as a fixture so the validator is grounded in
# something a model actually produced.
_HALLUCINATION_FEN = "r4bk1/2q2pp1/1p2p2n/2p1P2Q/b1PpP3/3P2PP/3B2BK/5RN1 w - - 1 24"
_HALLUCINATION_PROSE = (
    "The knight on f1 should improve its position with f1a1 to reroute "
    "the piece. Improving the coordination of the rooks and knights "
    "will be crucial for mounting an attack. The bishop on d2 currently "
    "defends important diagonals and might need to be redirected to "
    "challenge the center or kingside."
)


def test_false_claim_real_hallucination_fixture():
    # f1 actually holds a rook, not a knight; d2 actually holds the
    # bishop the model named, so it must not be flagged.
    board = chess.Board(_HALLUCINATION_FEN)
    result = find_false_piece_claims(_HALLUCINATION_PROSE, board)
    assert result == ["knight on f1"]


def test_true_claim_returns_empty():
    # Starting position: knight on g1 is a true claim.
    assert find_false_piece_claims("The knight on g1 develops next.", chess.Board()) == []


def test_empty_square_flagged():
    assert find_false_piece_claims("The bishop on e4 dominates.", chess.Board()) == ["bishop on e4"]


def test_wrong_piece_on_occupied_square_flagged():
    # Starting position f1 has a bishop, not a knight.
    assert find_false_piece_claims("The knight on f1 moves.", chess.Board()) == ["knight on f1"]


def test_color_mismatch_flagged():
    # Starting position: g1 has a white knight; claim says Black.
    result = find_false_piece_claims("Black knight on g1 sits.", chess.Board())
    assert result == ["black knight on g1"]


def test_color_match_passes():
    assert find_false_piece_claims("White knight on g1 develops.", chess.Board()) == []


def test_dedup_repeated_claims():
    text = "The knight on f1 moves. Then the knight on f1 retreats."
    assert find_false_piece_claims(text, chess.Board()) == ["knight on f1"]


def test_no_claim_returns_empty():
    assert find_false_piece_claims("The position is complicated.", chess.Board()) == []


def test_possessive_form_recognized():
    # "White's knight on g1" -- possessive with apostrophe-s.
    assert find_false_piece_claims("White's knight on g1 hops.", chess.Board()) == []
    assert find_false_piece_claims("White's bishop on g1 hops.", chess.Board()) == ["white bishop on g1"]


# ---------- "<square> <piece>" form (no "on") -------------------------
# Real chess prose often drops "on": "the b5 pawn", "the f3 knight",
# "White's e5 pawn". Captured from a real model transcript that the
# stricter "X on Y" regex missed.


def test_square_piece_form_true_claim_passes():
    # Starting position: g1 has a white knight.
    assert find_false_piece_claims("Develop the g1 knight.", chess.Board()) == []


def test_square_piece_form_false_claim_flagged():
    # Starting position: there is no piece on e4.
    assert find_false_piece_claims("The e4 bishop is strong.", chess.Board()) == ["bishop on e4"]


def test_square_piece_form_wrong_piece_flagged():
    # f1 holds a bishop, not a knight.
    assert find_false_piece_claims("The f1 knight is bad.", chess.Board()) == ["knight on f1"]


def test_square_piece_form_with_color_match():
    assert find_false_piece_claims("White's g1 knight develops.", chess.Board()) == []


def test_square_piece_form_with_color_mismatch_flagged():
    # g1 holds a white knight; claim says Black.
    assert find_false_piece_claims("Black's g1 knight sits.", chess.Board()) == ["black knight on g1"]


def test_square_piece_form_real_hallucination_fixture():
    # Real prose from a model that the stricter regex missed:
    # "the b5 pawn is hanging" against a board with a *black* p on b5.
    fen = "r4bk1/2q2p2/4p2p/1pp1P2Q/b1PpP3/3P1NPP/6BK/5R2 w - - 0 26"
    board = chess.Board(fen)
    # Bare "b5 pawn" -- color unspecified -- matches the black pawn:
    # not flagged.
    assert find_false_piece_claims("the b5 pawn is hanging", board) == []
    # "White's b5 pawn" -- color mismatch -- flagged.
    result = find_false_piece_claims("White's b5 pawn is hanging.", board)
    assert result == ["white pawn on b5"]


# ---------- Castle-word validator -------------------------------------
# Models often write "castle" / "castling" / "castles" instead of the
# SAN "O-O" / "O-O-O". When neither side can legally castle the prose
# is hallucinating that option.


def test_castle_word_passes_when_castling_is_legal():
    # Starting position: castling rights present but blocked. After we
    # clear the back rank for both sides, castling is legal.
    board = chess.Board("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")
    assert find_castle_word_violations("White should castle now.", board) == []
    assert find_castle_word_violations("Both sides will castle soon.", board) == []


def test_castle_word_flagged_when_neither_side_can_castle():
    # No castling rights -- the kings are not on their starting squares.
    board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    assert find_castle_word_violations("White should castle now.", board) == ["castle"]


def test_castle_word_dedup_keeps_first_occurrence():
    board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    text = "White wants to castle. After castling, the king is safer."
    # Distinct surface forms both flagged once.
    assert find_castle_word_violations(text, board) == ["castle", "castling"]


def test_castle_word_no_match_returns_empty():
    board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    assert find_castle_word_violations("The position is dry.", board) == []


def test_castle_word_only_one_side_can_castle_still_passes():
    # White has rights; black does not. A bare "castle" mention can
    # refer to either side, so we pass when *any* side can castle.
    board = chess.Board("4k3/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQ - 0 1")
    assert find_castle_word_violations("Castling is in the air.", board) == []


