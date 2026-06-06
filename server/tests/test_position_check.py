"""Single-board prose checks in llm/position_check.

Pure functions over (text, board). A claim is flagged when it does not match
the current board (or a board reached by a move named in the prose). The
checks ask the model to clarify, so the bias is against false positives:
a legitimate past/hypothetical reference is the model's to explain.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.llm.position_check import (
    describe_square,
    find_false_piece_claims,
    find_illegal_continuations,
    find_illegal_moves,
    find_illegal_piece_moves,
    iter_false_claim_squares,
    projected_boards,
)


# A middlegame position used across the piece-claim cases. Black to move.
# Pieces: bishop f5, knight c3 (white) / d3 (black), queen d2 (white), etc.
_MIDGAME_FEN = "r2qr1k1/5ppp/p4n2/1pbP1bB1/8/2Nn1B2/PP1Q1PPP/1N1R1RK1 b - - 3 17"
# White to move; the black knight on b4 can play ...Nd3 (a Black reply).
_POV_FEN = "r2qr1k1/5ppp/p4n2/1pbP1bB1/1n6/N1N2B2/PP1Q1PPP/R4RK1 w - - 0 16"


def _board(fen: str) -> chess.Board:
    return chess.Board(fen)


# --- find_illegal_moves ----------------------------------------------------

def test_illegal_move_flagged():
    board = chess.Board()  # startpos, white to move
    assert find_illegal_moves("I will play Qxh7 winning", board) == ["Qxh7"]


def test_legal_move_not_flagged():
    assert find_illegal_moves("Nf3 develops the knight", chess.Board()) == []


def test_san_label_carve_out_not_flagged():
    # 'Nb1' names the knight already on b1 -- a label, not a move proposal.
    assert find_illegal_moves("the Nb1 knight is undeveloped", chess.Board()) == []


def test_phantom_capture_flagged():
    # 'Nxf3' marks a capture, but f3 is empty at startpos -> false capture.
    assert find_illegal_moves("threatening Nxf3", chess.Board()) == ["Nxf3"]


def test_disambiguated_san_label_carve_out_not_flagged():
    # 'Ngf3' names the knight already on f3 -- a disambiguated label, not a
    # move (the g1->f3 hop is illegal, f3 occupied). It must not be flagged.
    board = _board("4k3/8/8/8/8/5N2/8/4K1N1 w - - 0 1")  # knights f3 and g1
    assert find_illegal_moves("the Ngf3 knight anchors", board) == []


def test_ellipsis_prefix_validates_from_black_pov():
    # ...Nd3 is a Black reply (black knight b4->d3), legal even though it is
    # White to move. The "..." marks Black's POV, so it is not flagged.
    board = _board(_POV_FEN)
    assert find_illegal_moves("After ...Nd3 Black is better", board) == []


def test_move_illegal_for_side_to_move_flagged():
    # No "..." prefix -> validated from the side to move (Black). Qd4 is
    # illegal for Black here (the d-file is blocked), so it is flagged.
    board = _board(_MIDGAME_FEN)
    assert "Qd4" in find_illegal_moves("Black should consider Qd4", board)


# --- find_false_piece_claims / iter_false_claim_squares --------------------

def test_false_piece_claim_flagged():
    # h6 is empty and no black bishop can reach it -- a present-tense board
    # error with no reachable cover, so it is flagged.
    board = _board(_MIDGAME_FEN)
    assert find_false_piece_claims("the bishop on h6 eyes the king", board) == [
        "bishop on h6"
    ]


def test_reachable_square_claim_not_flagged():
    # g6 is empty, but the f5 bishop (side to move) can play to g6, so a
    # colorless "bishop on g6" claim is cleared by reachability, not flagged.
    board = _board(_MIDGAME_FEN)
    assert find_false_piece_claims("the bishop on g6 eyes the king", board) == []


def test_true_piece_claim_not_flagged():
    # Black's knight really is on d3 here.
    board = _board(_MIDGAME_FEN)
    assert find_false_piece_claims("Black's knight on d3 dominates", board) == []


def test_square_piece_phrasing_flagged():
    # "the c1 rook" phrasing (square then piece); c1 is empty here.
    board = _board(_MIDGAME_FEN)
    assert find_false_piece_claims("the c1 rook is active", board) == ["rook on c1"]


def test_claim_cleared_by_projected_board():
    # d3 is empty now, but "...Nd3" puts a knight there -- the forward-looking
    # claim validates against the projected position, so it is not flagged.
    board = _board(_POV_FEN)
    text = "After ...Nd3 Black establishes a knight on d3"
    assert find_false_piece_claims(text, board) == []


def test_colorless_claim_about_non_moving_side_flagged():
    # _POV_FEN is White to move; the black knight on b4 can reach d3, but a
    # bare "knight on d3" validates from the side to move (White), which cannot
    # reach d3. Strict POV: flagged absent a color word or a named move.
    board = _board(_POV_FEN)
    assert find_false_piece_claims("a knight on d3 is strong", board) == [
        "knight on d3"
    ]


def test_colored_claim_about_non_moving_side_cleared():
    # Same square, but "black knight on d3" names the color -> validated from
    # Black, who can reach d3 -> cleared. The color word picks the POV.
    board = _board(_POV_FEN)
    assert find_false_piece_claims("a black knight on d3 is strong", board) == []


def test_wrong_color_claim_flagged():
    # A white knight is on c3; claiming a black knight there is a color error.
    board = _board(_MIDGAME_FEN)
    assert find_false_piece_claims("black knight on c3", board) == [
        "black knight on c3"
    ]


def test_iter_false_claim_squares_yields_surface_label_square():
    # Surface keeps the exact prose ("the rook on c1"); label is normalized.
    # h6 and c1 are both empty and unreachable here -> both flagged.
    board = _board(_MIDGAME_FEN)
    rows = list(iter_false_claim_squares("the bishop on h6 and rook on c1", board))
    assert rows == [
        ("the bishop on h6", "bishop on h6", "h6"),
        ("rook on c1", "rook on c1", "c1"),
    ]


def test_false_claim_surface_keeps_possessive():
    # The strike target must be the exact prose ("White's knight on a1"), not
    # the normalized label ("white knight on a1") -- they differ. a1 is empty
    # and no white knight can reach it, so the claim is flagged.
    board = _board("r2qr1k1/5ppp/p4n2/1pbP1bB1/8/2Nn1B2/PP1Q1PPP/3R1RK1 w - - 0 1")
    rows = list(iter_false_claim_squares("White's knight on a1 is passive", board))
    assert rows == [("White's knight on a1", "white knight on a1", "a1")]


# --- find_illegal_piece_moves ('<piece> to <square>') ---------------------

def test_piece_to_unreachable_square_flagged():
    # No bishop can reach a1 here -> the prose move is impossible.
    board = _board(_MIDGAME_FEN)
    assert find_illegal_piece_moves("the bishop goes to a1", board) == [
        "bishop to a1"
    ]


def test_piece_to_reachable_square_not_flagged():
    # The bishop on f5 can play Bg6 -> a legitimate plan, not flagged.
    board = _board(_MIDGAME_FEN)
    assert find_illegal_piece_moves("the bishop swings to g6", board) == []


def test_piece_to_bare_phrasing_flagged():
    # "rook to h8" with no movement verb still parses as a move phrase.
    board = _board(_MIDGAME_FEN)
    assert find_illegal_piece_moves("rook to h8 wins", board) == ["rook to h8"]


def test_non_move_to_phrase_not_matched():
    # "tied to" / "according to" / "pinned to" are not move phrases.
    board = _board(_MIDGAME_FEN)
    assert find_illegal_piece_moves("the bishop tied to the defense", board) == []
    assert find_illegal_piece_moves("the pawn pinned to the king", board) == []


@pytest.mark.parametrize("verb", [
    "redirect", "redirects", "redirecting", "redirection",
    "direct", "directs", "directing",
    "deploy", "deploys", "deploying", "deployment",
    "redeploy", "redeploys", "redeployment",
])
def test_redirect_deploy_verbs_flagged(verb):
    # [re]direct / [re]deploy + any suffix reads as a move phrase. a1 is
    # unreachable by any bishop here, so the move is flagged.
    board = _board(_MIDGAME_FEN)
    assert find_illegal_piece_moves(f"bishop {verb} to a1", board) == [
        "bishop to a1"
    ]


def test_redirect_deploy_to_reachable_square_not_flagged():
    # The f5 bishop can reach g6 -> a redirect plan that is legal, not flagged.
    board = _board(_MIDGAME_FEN)
    assert find_illegal_piece_moves("bishop redeployment to g6", board) == []


def test_redirect_deploy_without_square_not_matched():
    # The \w* verb still needs a bare square after "to"; "to defend" / "to the
    # kingside" are not move phrases and must not match.
    board = _board(_MIDGAME_FEN)
    assert find_illegal_piece_moves("bishop directed to defend", board) == []
    assert find_illegal_piece_moves("redirection of play to the kingside", board) == []


# --- find_illegal_continuations -------------------------------------------

def test_broken_continuation_flagged():
    # From startpos, Bb5 is illegal (e-pawn unmoved) -> the line does not play.
    board = chess.Board()
    assert find_illegal_continuations("We try Nf3 Nc6 Bb5 a6 here", board) == [
        "Nf3 Nc6 Bb5 a6"
    ]


def test_legal_continuation_not_flagged():
    # Ruy Lopez: after 1.e4 e5 2.Nf3 Nc6 3.Bb5, a6 Ba4 Nf6 plays cleanly.
    board = chess.Board()
    for m in ["e4", "e5", "Nf3", "Nc6", "Bb5"]:
        board.push_san(m)
    assert find_illegal_continuations("Main line: a6 Ba4 Nf6 O-O", board) == []


def test_white_only_numbered_shorthand_skipped():
    # "1.e4 2.Nf3" omits Black's plies -> not a replayable sequence, skipped.
    board = chess.Board()
    assert find_illegal_continuations("the plan 1.e4 2.Nf3 develops", board) == []


# --- projected_boards ------------------------------------------------------

def test_projected_boards_one_hop_per_valid_move():
    board = _board(_POV_FEN)
    # "...Nd3" (black) and "Rad1" (white STM) are both valid -> two hops.
    boards = projected_boards("Rad1 is good; after ...Nd3 Black holds", board)
    assert len(boards) == 2
    # The black-POV hop has a knight on d3.
    assert any(
        b.piece_at(chess.D3) and b.piece_at(chess.D3).piece_type == chess.KNIGHT
        for b in boards
    )


def test_projected_boards_skips_unparseable_independently():
    # An unparseable token is skipped, not a sequence-breaker: a later valid
    # move still projects from the live board (1-ply hops are independent).
    board = chess.Board()
    boards = projected_boards("Nf3 puts a knight on f3", board)
    assert len(boards) == 1
    assert boards[0].piece_at(chess.F3).piece_type == chess.KNIGHT


# --- describe_square -------------------------------------------------------

def test_describe_square_empty():
    assert describe_square("e4", chess.Board()) == "e4 is empty"


def test_describe_square_occupied():
    assert describe_square("g1", chess.Board()) == "g1 has a white knight"
