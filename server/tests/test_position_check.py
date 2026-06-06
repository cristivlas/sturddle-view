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
    find_illegal_pawn_moves,
    find_illegal_piece_moves,
    find_illegal_square_moves,
    handled_continuation_spans,
    iter_false_claim_squares,
    iter_illegal_continuations,
    iter_illegal_moves,
    iter_illegal_piece_moves,
    iter_illegal_square_moves,
    projected_boards,
    truncate_at_future_line,
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


def test_numbered_move_for_other_move_not_flagged():
    # "17.Nab1" carries a move number that is not the current fullmove (16),
    # so it cites a past/hypothetical line, not the live board -- left alone
    # even though Nab1 is illegal here.
    board = _board("r2qr1k1/5ppp/p4n2/1pbP1bB1/1n6/N1N2B2/PP1Q1PPP/3R1RK1 b - - 1 16")
    assert find_illegal_moves("after 17.Nab1 Karpov was left with", board) == []


def test_numbered_move_for_current_move_flagged():
    # "16...Nab1" is the live ply (current fullmove and Black to move), so it
    # is checked -- Nab1 is illegal and flagged.
    board = _board("r2qr1k1/5ppp/p4n2/1pbP1bB1/1n6/N1N2B2/PP1Q1PPP/3R1RK1 b - - 1 16")
    assert find_illegal_moves("the move 16...Nab1 fails", board) == ["Nab1"]


def test_just_played_move_at_current_number_not_flagged():
    # White's 19th was just played (now Black to move at fullmove 19), so
    # "19.Ke2" cites that already-played move -- the number matches the current
    # fullmove but the color isn't to move, so it is not flagged.
    board = _board("rnb1k1nr/p2p1ppp/3B4/1pbN1N1P/4P1P1/3P1Q2/P1P1K3/q5R1 b kq - 1 19")
    assert find_illegal_moves("after 18...Qxa1+ 19.Ke2 Black played Bxg1", board) == []


def test_numbered_move_matching_history_not_flagged():
    # "3.Bb5" was actually played; even though it is illegal on the current
    # board, a numbered move matching the move_stack gets a free pass.
    board = chess.Board()
    for m in ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6"]:
        board.push_san(m)
    assert find_illegal_moves("the pin with 3.Bb5 was strong", board) == []
    assert find_illegal_moves("Black replied 2...Nc6 developing", board) == []


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


@pytest.mark.parametrize("verb", ["captured", "took", "exchanged", "traded", "sacrificed"])
def test_captured_piece_claim_not_flagged(verb):
    # A capture verb marks a past event ("captured the rook on h6"), not a
    # live-board claim -- h6 is empty/unreachable but the claim is skipped.
    board = _board(_MIDGAME_FEN)
    assert find_false_piece_claims(f"White {verb} the rook on h6 earlier", board) == []
    assert find_false_piece_claims(f"White {verb} the h6 rook earlier", board) == []


def test_non_capture_verb_still_flags():
    # An ordinary verb before the claim ("planted the rook on h6") is not a
    # capture, so the false claim is still flagged.
    board = _board(_MIDGAME_FEN)
    assert find_false_piece_claims("Black planted the rook on h6", board) == ["rook on h6"]


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


# --- find_illegal_square_moves ('<square> to <square>') -------------------

_SQ_FEN = "r1br2k1/pp2qppp/2n1p3/2pn4/2NP4/4QNP1/PP2PPBP/R2R2K1 w - - 5 13"


def test_square_move_unreachable_flagged():
    # g2 holds a bishop that cannot reach b3 -> the prose move is impossible.
    board = _board(_SQ_FEN)
    assert find_illegal_square_moves("g2 moves to b3 fails", board) == ["g2 to b3"]


def test_square_move_from_and_bare_phrasings_flagged():
    # "from g2 to b3" and bare "g2 to b3" both read as the same move phrase.
    board = _board(_SQ_FEN)
    assert find_illegal_square_moves("the bishop from g2 to b3", board) == ["g2 to b3"]
    assert find_illegal_square_moves("g2 to b3", board) == ["g2 to b3"]


def test_legal_square_move_not_flagged():
    # The c4 knight can play to e5 -> a legitimate move, not flagged.
    board = _board(_SQ_FEN)
    assert find_illegal_square_moves("c4 to e5 jumps", board) == []


def test_empty_source_square_move_flagged():
    # b3 is empty -> nothing can move from it.
    board = _board(_SQ_FEN)
    assert find_illegal_square_moves("b3 to c2", board) == ["b3 to c2"]


def test_promotion_move_not_flagged():
    # A pawn to the back rank is legal via promotion; the move-legality check
    # must allow it ("pawn to e8", "e7 to e8") rather than flag a phantom.
    board = _board("8/4P3/8/8/8/8/8/K6k w - - 0 1")  # white pawn e7
    assert find_illegal_piece_moves("the pawn to e8 queens", board) == []
    assert find_illegal_square_moves("e7 to e8", board) == []


def _run_move_checks(text, board):
    # Mirror the coordinator: one shared dedup set across the move recognizers.
    seen: set[str] = set()
    pairs = (
        list(iter_illegal_moves(text, board, seen))
        + list(iter_illegal_piece_moves(text, board, seen))
        + list(iter_illegal_square_moves(text, board, seen))
    )
    return [label for _surface, label in pairs]


def test_same_move_flagged_once_across_recognizers():
    # "g2 to b3" (square form) and "bishop to b3" (piece form) name the same
    # impossible move; the shared uci key (g2b3) dedups them to one flag. The
    # piece recognizer runs before the square one, so its label wins.
    board = _board(_SQ_FEN)
    assert _run_move_checks("g2 to b3 and the bishop to b3 both fail", board) == [
        "bishop to b3"
    ]


# --- find_illegal_pawn_moves ('move e4') ----------------------------------

def test_move_word_illegal_pawn_push_flagged():
    # "e5" reads as a square in prose, but "move e5" marks it a move; e5 is
    # illegal for White at startpos, so it is flagged.
    assert find_illegal_pawn_moves("the move e5 here", chess.Board()) == ["e5"]


def test_move_word_legal_pawn_push_not_flagged():
    # "move e4" is a legal push at startpos -> not flagged.
    assert find_illegal_pawn_moves("the move e4 opens", chess.Board()) == []


def test_bare_pawn_square_without_move_word_ignored():
    # Without "move", a bare pawn square is a square reference, not a move.
    assert find_illegal_pawn_moves("e5 is a weak square", chess.Board()) == []


# --- find_illegal_continuations -------------------------------------------

def test_broken_continuation_flags_breaking_move_only():
    # From startpos: Nf3, Nc6 play; Bb5 is illegal (e-pawn unmoved) -> the line
    # breaks at Bb5. Only that move is flagged -- Nf3/Nc6 are valid, and moves
    # after the break can't be judged from a position never legally reached.
    board = chess.Board()
    assert find_illegal_continuations("We try Nf3 Nc6 Bb5 a6 here", board) == ["Bb5"]


def test_broken_continuation_surface_and_span_isolate_the_move():
    # The struck surface and its char span cover only the breaking move, so the
    # UI strikes Bb5 alone, not the whole run.
    board = chess.Board()
    text = "We try Nf3 Nc6 Bb5 a6 here"
    out = list(iter_illegal_continuations(text, board))
    assert len(out) == 1
    surface, label, span = out[0]
    assert surface == "Bb5"
    assert label == "Bb5"
    assert text[span[0]:span[1]] == "Bb5"


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


# A numbered run anchored at the current move: 24.Nf6+ Qxf6 25.Qc7 ... 25...Bg4.
# Qxf6 is Black's reply with no "..." (the number sits on White's move), so the
# per-token check must not validate it from White's POV and flag it.
_PAIR_FEN = "r1b2rk1/pp2qp1p/1n4p1/4p3/4P1N1/P1Q3P1/1P3P1P/2RR1BK1 w - - 0 24"


def _all_flags(text: str, board: chess.Board) -> list[str]:
    # Mirror the coordinator: the line check claims spans of runs it handles, so
    # the per-token recognizer skips a numbered pair's moves inside them.
    spans = handled_continuation_spans(text, board)
    moves = [lbl for _s, lbl in iter_illegal_moves(text, board, None, spans)]
    lines = [lbl for _s, lbl, _sp in iter_illegal_continuations(text, board)]
    return moves + lines


def test_black_reply_in_numbered_pair_not_flagged():
    board = _board(_PAIR_FEN)
    text = (
        "The check on f6 backfires catastrophically. After 24.Nf6+ Qxf6 "
        "25.Qc7, Black has 25...Bg4, attacking the rook and pinning it to "
        "the king -- White's queen on c7 cannot defend. The knight "
        "sacrifice collapses."
    )
    text = truncate_at_future_line(text, board)
    assert _all_flags(text, board) == []


def test_black_reply_in_bare_pair_not_flagged():
    board = _board(_PAIR_FEN)
    text = (
        "After 24.Nf6+ Qxf6, Black's queen captures on f6, not White's. The "
        "key point stands: 24.Nf6+ fails because Black has 25...Bg4, "
        "attacking the rook."
    )
    text = truncate_at_future_line(text, board)
    assert _all_flags(text, board) == []


def _played(sans: list[str]) -> chess.Board:
    board = chess.Board()
    for san in sans:
        board.push_san(san)
    return board


def test_past_run_anchored_via_history_not_flagged():
    # After 1.e4 e5 2.Nf3 it is move 2, Black to move. "1.e4 e5" replays from
    # the popped move-1 position, not the live board (where e4 is illegal).
    board = _played(["e4", "e5", "Nf3"])
    assert _all_flags("the symmetric 1.e4 e5 opening", board) == []


def test_past_run_illegal_at_its_anchor_flagged():
    # Anchored at move 1, "1.e4 e5" plays; "2.Nf6" breaks there (no knight can
    # reach f6), so only the breaking move is flagged, not the valid prefix.
    board = _played(["e4", "e5", "Nf3"])
    assert _all_flags("the line 1.e4 e5 2.Nf6", board) == ["Nf6"]


def test_run_before_stack_base_skipped():
    # Imported position (FEN, no history) at move 24; a run citing move 5
    # predates the stack base and cannot be anchored -> left alone.
    board = _board(_PAIR_FEN)
    assert _all_flags("recall 5.Bb5 a6 from the opening", board) == []


def test_black_led_past_run_anchored_at_black_ply():
    # After 1.e4 e5 2.Nf3 Nc6 it is move 3, White to move. "2...Nc6 3.Bb5"
    # leads with Black's move 2 and must anchor there (after 2.Nf3), where Nc6
    # is legal; anchoring at White's move 2 would misjudge it.
    board = _played(["e4", "e5", "Nf3", "Nc6"])
    assert _all_flags("the line 2...Nc6 3.Bb5 holds", board) == []


def test_black_led_run_illegal_at_its_anchor_flagged():
    # Same anchor (after 2.Nf3): Nc6 plays, Bxc6 is illegal there (bishop can't
    # reach c6), so only the breaking move Bxc6 is flagged.
    board = _played(["e4", "e5", "Nf3", "Nc6"])
    assert _all_flags("the line 2...Nc6 3.Bxc6 fails", board) == ["Bxc6"]


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


# --- truncate_at_future_line -----------------------------------------------

_FUTURE_FEN = "rnb1k1nr/p2p1ppp/3B4/1pbN1N1P/4P1P1/3P1Q2/P1P1K3/q5R1 b kq - 1 19"


def test_future_line_forgoes_rest_of_prose():
    # At move 19, "20.Nf3 ..." enters a hypothetical line; everything from the
    # future move number on is dropped, so its bogus claims aren't flagged.
    board = _board(_FUTURE_FEN)
    text = "Black is solid. Then 20.Nf3 wins the rook on h6 after Bxa1."
    assert truncate_at_future_line(text, board) == "Black is solid. Then "
    assert find_false_piece_claims(text[: len("Black is solid. Then ")], board) == []


def test_current_and_past_numbers_do_not_truncate():
    # Numbers at or below the current fullmove keep the prose intact.
    board = _board(_FUTURE_FEN)
    text = "After 18...Qxa1+ 19.Ke2 Black is winning."
    assert truncate_at_future_line(text, board) == text
