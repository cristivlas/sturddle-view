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
    find_illegal_continuations,
    find_illegal_moves,
    find_move_attribution_errors,
)
from sturddle_view.play.ai_analysis import _boards_for_validation


def test_empty_text_returns_empty():
    assert find_illegal_moves("", [chess.Board()]) == []


def test_no_move_tokens_returns_empty():
    text = "The position is balanced, both sides have chances."
    assert find_illegal_moves(text, [chess.Board()]) == []




def test_legal_piece_move_passes():
    assert find_illegal_moves("Develop with Nf3.", [chess.Board()]) == []


def test_bare_pawn_move_not_flagged():
    # Bare-square pawn moves (e4, e5, h6, ...) are out of scope: chess
    # prose mentions squares as description constantly. Loss accepted.
    assert find_illegal_moves("White plays e5.", [chess.Board()]) == []
    assert find_illegal_moves("The h6 pawn is weak.", [chess.Board()]) == []


def test_illegal_piece_move_detected():
    # Nf6 is on a black square; the white knight from g1 can't reach it.
    assert find_illegal_moves("White plays Nf6.", [chess.Board()]) == ["Nf6"]


def test_duplicate_illegal_move_listed_once():
    text = "Try Nf6. No really, Nf6 is the move."
    assert find_illegal_moves(text, [chess.Board()]) == ["Nf6"]


def test_annotation_glyphs_preserved_in_token():
    # The token includes the glyph so the corrective message echoes
    # what the model wrote.
    assert find_illegal_moves("White plays Nf6!?.", [chess.Board()]) == ["Nf6!?"]


def test_mixed_legal_and_illegal():
    text = "Nf3 is fine, but Nf6 is wrong, and e4 works."
    assert find_illegal_moves(text, [chess.Board()]) == ["Nf6"]


def test_invalid_san_ignored_not_flagged():
    # "Xyz" doesn't match the regex; "Nzz" matches piece-prefix but
    # parse_san raises InvalidMoveError, not IllegalMoveError.
    text = "The position is complex; consider Xyz or other ideas."
    assert find_illegal_moves(text, [chess.Board()]) == []


def test_castling_legal_in_appropriate_position():
    board = chess.Board("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")
    assert find_illegal_moves("White can play O-O.", [board]) == []


def test_castling_illegal_when_blocked():
    # Starting position: castling is illegal (pieces in the way).
    assert find_illegal_moves("White plays O-O.", [chess.Board()]) == ["O-O"]


@pytest.mark.parametrize("token", ["Nf3", "Nc3"])
def test_legal_piece_first_moves_pass(token):
    assert find_illegal_moves(f"Consider {token}.", [chess.Board()]) == []


def test_illegal_pawn_capture_detected():
    # Pawn captures keep the file prefix (`exd5`), so they are
    # syntactically distinct from bare-square prose and remain in scope.
    assert find_illegal_moves("White plays exd5.", [chess.Board()]) == ["exd5"]


# A real reviewed position (black to move). Black king g8, rooks a8/e8,
# queen d8; d1/d3/f4 empty. Reused across several hallucination fixtures.
_REVIEWED_FEN = "r2qr1k1/5ppp/p4n2/1pbP1bB1/1n6/N1N2B2/PP1Q1PPP/3R1RK1 b - - 1 16"


def test_phantom_capture_flagged():
    # Real model prose: 'Bxe4' marks a capture, but e4 is empty -- f5-e4 is a
    # quiet move that python-chess parses leniently. The validator must flag
    # the false capture; the legal alternative Rac8 in the same text does not.
    board = chess.Board(_REVIEWED_FEN)
    text = (
        "Bxe4 immediately challenges the central pawn structure and gains a "
        "tempo against the diagonal bishop on g5. Black's knight on b4 "
        "pressures the c2 pawn, and the initiative arising from the bishop "
        "trade can open lines towards the white king's position. A strong "
        "alternative to Bxe4 exists utilizing the rooks for pressure, such as "
        "Rac8, but Bxe4 forces the most immediate structural concession."
    )
    assert find_illegal_moves(text, [board]) == ["Bxe4"]
    # Same move without the bogus capture mark is a legal quiet move.
    assert find_illegal_moves("Be4 challenges the center.", [board]) == []


def test_real_capture_not_flagged():
    # Nxd5 takes the white pawn on d5 -- a genuine capture, not flagged.
    board = chess.Board(_REVIEWED_FEN)
    assert find_illegal_moves("Nxd5 wins a pawn.", [board]) == []


def test_en_passant_capture_not_flagged():
    # exf6 is en passant -- is_capture() is True, so the capture mark holds.
    board = chess.Board("rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3")
    assert find_illegal_moves("exf6 wins the pawn.", [board]) == []


# ---------- Move-numbered reply exemption -----------------------------
# A SAN prefixed with a move number names its side: "17." -> White,
# "16..." -> Black. A numbered move legal for that side is a correctly
# attributed reply, not a live-board move, even when illegal as-is on the
# current board (the other side is to move). Real prose: black to move at
# ply 16 (_REVIEWED_FEN), prose cites White's "17.Nab1" reply.


def test_numbered_reply_for_other_side_not_flagged():
    # Black to move; "17.Nab1" is White's legal reply (a3->b1). Exempt.
    board = chess.Board(_REVIEWED_FEN)
    assert find_illegal_moves("White's 17.Nab1 reply drifts into passivity.", [board]) == []


def test_numbered_move_for_side_to_move_not_flagged():
    # "16...Nd3" is Black's (side to move) legal move -- exempt as usual.
    board = chess.Board(_REVIEWED_FEN)
    assert find_illegal_moves("Kasparov's 16...Nd3 is the right call.", [board]) == []


def test_unnumbered_move_for_other_side_still_flagged():
    # Bare "Nab1" (no move number) on a black-to-move board is flagged --
    # the exemption needs the number to name the side.
    board = chess.Board(_REVIEWED_FEN)
    assert find_illegal_moves("Nab1 is bad.", [board]) == ["Nab1"]


def test_numbered_but_illegal_reply_still_flagged():
    # "17.Rd8" is numbered White but illegal (d1 rook blocked by the d5
    # pawn); the number does not excuse an illegal move.
    board = chess.Board(_REVIEWED_FEN)
    assert find_illegal_moves("Then 17.Rd8 wins.", [board]) == ["Rd8"]


def test_hallucinated_check_sequence_flagged():
    # Real model prose that confused this position for a different game:
    # invents a check (Rxd1+), king escapes the wrong king can't make
    # (Kh2/Kg1 -- black king is on g8), queen moves the queen can't reach
    # (Qf1+/Qf2), and a rook on the empty f4. Every fabricated move/piece
    # is flagged; the only legitimate mention (the knight on d3, reachable)
    # is not.
    board = chess.Board(_REVIEWED_FEN)
    text = (
        "Black has a formidable knight on d3 that dominates the board. The "
        "rook has captured on d1 with check (40...Rxd1+). The only legal "
        "move to exit check is Kh2 (or Kg1), which runs into Qf1+ and Qf2 "
        "mating patterns. After Kh2, the undefended rook on f4 gives Black a "
        "decisive advantage."
    )
    assert find_illegal_moves(text, [board]) == ["Rxd1+", "Kh2", "Kg1", "Qf1+", "Qf2"]
    assert find_false_piece_claims(text, [board]) == ["rook on f4"]


def test_illegal_queen_move_in_prose_detected():
    # Regression: model called validate_move(Qg4) -> illegal, then wrote
    # prose recommending "Qg4" anyway. Round-end validator must flag it.
    text = (
        "A strong candidate is Qg4, which develops the queen with tempo "
        "and eyes at f7."
    )
    assert find_illegal_moves(text, [chess.Board()]) == ["Qg4"]


def test_san_label_not_flagged_when_piece_already_on_square():
    # 'Qd1' in prose IS the queen's current square in the startpos;
    # treat as a label, not an illegal move-to-own-square.
    text = "The queen at Qd1 supports the center."
    assert find_illegal_moves(text, [chess.Board()]) == []


def test_san_label_carveout_requires_piece_type_match():
    # 'Kd1' would name a king on d1, but d1 holds the queen -> not a
    # label; parse_san would also reject as illegal. Validator flags.
    text = "Consider Kd1 to relocate the king."
    assert find_illegal_moves(text, [chess.Board()]) == ["Kd1"]


def test_san_label_carveout_requires_side_to_move_owns_piece():
    # White to move, but the queen on d8 is Black's. 'Qd8' is not a
    # label for White's side; if parse_san rejects as illegal, flag.
    text = "Then Qd8 controls the back rank."
    assert find_illegal_moves(text, [chess.Board()]) == ["Qd8"]


def test_san_label_carveout_only_for_three_char_shape():
    # 'Qxd1+' captures-with-check; even if d1 holds the side-to-move
    # queen, the token isn't a bare piece+square label -- it's a move
    # claim with capture semantics. Should still be flagged when illegal.
    text = "After Qxd1+ the king is exposed."
    out = find_illegal_moves(text, [chess.Board()])
    assert out == ["Qxd1+"]


# Immortal-Game position, Black to move; White has Nd5, Nf5, Bd6.
_NDNF_FEN = "rnb1k1nr/p2p1ppp/3B4/1pbN1N1P/4P1P1/3P1Q2/P1P1K3/q5R1 b kq - 1 19"


def test_possessive_color_label_for_opponents_piece_not_flagged():
    # Black to move, but "White's Nd5" names White's knight actually on d5
    # -- a label, not a Black move claim. Must not flag.
    board = chess.Board(_NDNF_FEN)
    text = "White's Nd5 dominates the center."
    assert find_illegal_moves(text, [board]) == []


def test_possessive_color_label_applies_across_a_list():
    # The color word governs the whole list: Nf5 and Bd6 inherit "White's".
    board = chess.Board(_NDNF_FEN)
    text = "White's Nd5, Nf5, and Bd6 dominate every key square."
    assert find_illegal_moves(text, [board]) == []


def test_possessive_color_label_requires_piece_actually_there():
    # "White's Qd5" -- d5 holds a knight, not a queen -> not a valid label,
    # and Qd5 is no legal move here, so it flags.
    board = chess.Board(_NDNF_FEN)
    text = "White's Qd5 is decisive."
    assert find_illegal_moves(text, [board]) == ["Qd5"]


def test_bare_token_without_color_word_still_flagged():
    # No possessive color -> "Nd5" reads as a move claim, illegal for Black
    # here, so it flags (the carve-out needs the explicit color word).
    board = chess.Board(_NDNF_FEN)
    text = "Then Nd5 takes over."
    assert find_illegal_moves(text, [board]) == ["Nd5"]


def test_possessive_wrong_color_label_flagged():
    # White to move, white knight on d5. Prose says "Black's Nd5" -- the
    # piece is White's, so the wrong-color label must flag, not be excused
    # by the same-side label carve-out.
    board = chess.Board("4k3/8/8/3N4/8/8/8/4K3 w - - 0 1")
    text = "Black's Nd5 anchors the position."
    assert find_illegal_moves(text, [board]) == ["Nd5"]


def test_possessive_color_legal_move_still_passes():
    # "White's Nf3" with White to move: Nf3 is a legal move (no knight on
    # f3 yet), so the color word must not turn a legal move into a flag.
    text = "White's Nf3 develops with tempo."
    assert find_illegal_moves(text, [chess.Board()]) == []


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
    result = find_false_piece_claims(_HALLUCINATION_PROSE, [board])
    assert result == ["knight on f1"]


def test_true_claim_returns_empty():
    # Starting position: knight on g1 is a true claim.
    assert find_false_piece_claims("The knight on g1 develops next.", [chess.Board()]) == []


def test_empty_square_flagged():
    assert find_false_piece_claims("The bishop on e4 dominates.", [chess.Board()]) == ["bishop on e4"]


def test_wrong_piece_on_occupied_square_flagged():
    # Starting position f1 has a bishop, not a knight.
    assert find_false_piece_claims("The knight on f1 moves.", [chess.Board()]) == ["knight on f1"]


def test_color_mismatch_flagged():
    # Starting position: g1 has a white knight; claim says Black.
    result = find_false_piece_claims("Black knight on g1 sits.", [chess.Board()])
    assert result == ["black knight on g1"]


def test_color_match_passes():
    assert find_false_piece_claims("White knight on g1 develops.", [chess.Board()]) == []


def test_dedup_repeated_claims():
    text = "The knight on f1 moves. Then the knight on f1 retreats."
    assert find_false_piece_claims(text, [chess.Board()]) == ["knight on f1"]


def test_no_claim_returns_empty():
    assert find_false_piece_claims("The position is complicated.", [chess.Board()]) == []


def test_possessive_form_recognized():
    # "White's knight on g1" -- possessive with apostrophe-s.
    assert find_false_piece_claims("White's knight on g1 hops.", [chess.Board()]) == []
    assert find_false_piece_claims("White's bishop on g1 hops.", [chess.Board()]) == ["white bishop on g1"]


# ---------- "<square> <piece>" form (no "on") -------------------------
# Real chess prose often drops "on": "the b5 pawn", "the f3 knight",
# "White's e5 pawn". Captured from a real model transcript that the
# stricter "X on Y" regex missed.


def test_square_piece_form_true_claim_passes():
    # Starting position: g1 has a white knight.
    assert find_false_piece_claims("Develop the g1 knight.", [chess.Board()]) == []


def test_square_piece_form_false_claim_flagged():
    # Starting position: there is no piece on e4.
    assert find_false_piece_claims("The e4 bishop is strong.", [chess.Board()]) == ["bishop on e4"]


def test_square_piece_form_wrong_piece_flagged():
    # f1 holds a bishop, not a knight.
    assert find_false_piece_claims("The f1 knight is bad.", [chess.Board()]) == ["knight on f1"]


def test_square_piece_form_with_color_match():
    assert find_false_piece_claims("White's g1 knight develops.", [chess.Board()]) == []


def test_square_piece_form_with_color_mismatch_flagged():
    # g1 holds a white knight; claim says Black.
    assert find_false_piece_claims("Black's g1 knight sits.", [chess.Board()]) == ["black knight on g1"]


def test_square_piece_form_real_hallucination_fixture():
    # Real prose from a model that the stricter regex missed:
    # "the b5 pawn is hanging" against a board with a *black* p on b5.
    fen = "r4bk1/2q2p2/4p2p/1pp1P2Q/b1PpP3/3P1NPP/6BK/5R2 w - - 0 26"
    board = chess.Board(fen)
    # Bare "b5 pawn" -- color unspecified -- matches the black pawn:
    # not flagged.
    assert find_false_piece_claims("the b5 pawn is hanging", [board]) == []
    # "White's a6 pawn" -- wrong color and unreachable by any white move --
    # flagged.
    result = find_false_piece_claims("White's a6 pawn is hanging.", [board])
    assert result == ["white pawn on a6"]


# ---------- Castle-word validator -------------------------------------
# Models often write "castle" / "castling" / "castles" instead of the
# SAN "O-O" / "O-O-O". When neither side can legally castle the prose
# is hallucinating that option.


def test_castle_word_passes_when_castling_is_legal():
    # Starting position: castling rights present but blocked. After we
    # clear the back rank for both sides, castling is legal.
    board = chess.Board("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")
    assert find_castle_word_violations("White should castle now.", [board]) == []
    assert find_castle_word_violations("Both sides will castle soon.", [board]) == []


def test_castle_word_flagged_when_neither_side_can_castle():
    # No castling rights -- the kings are not on their starting squares.
    board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    assert find_castle_word_violations("White should castle now.", [board]) == ["castle"]


def test_castle_word_dedup_keeps_first_occurrence():
    board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    text = "White wants to castle. After castling, the king is safer."
    # Distinct surface forms both flagged once.
    assert find_castle_word_violations(text, [board]) == ["castle", "castling"]


def test_castle_word_no_match_returns_empty():
    board = chess.Board("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    assert find_castle_word_violations("The position is dry.", [board]) == []


def test_castle_word_only_one_side_can_castle_still_passes():
    # White has rights; black does not. A bare "castle" mention can
    # refer to either side, so we pass when *any* side can castle.
    board = chess.Board("4k3/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQ - 0 1")
    assert find_castle_word_violations("Castling is in the air.", [board]) == []


# ---------- Move-target carve-out (forward-looking prose) -------------
# Real case: model recommends a move and then describes the resulting
# state -- "play Re1 ... the rook on e1 supports the file". The live
# board has no rook on e1 yet; the carve-out keeps the validator from
# flagging the post-move description as a false claim.


def test_move_target_carveout_rook_to_e1():
    # White's f1-rook can move to e1 from startpos (... no, blocked).
    # Use a position where Re1 is legal.
    board = chess.Board("4k3/8/8/8/8/8/8/R3K3 w - - 0 1")  # white rook a1, king e1
    # Reposition: white rook on f1, king g1, so Re1 is legal.
    board = chess.Board("4k3/8/8/8/8/8/8/5RK1 w - - 0 1")
    text = "Play Re1 to centralize. The rook on e1 supports the file."
    assert find_false_piece_claims(text, [board]) == []


def test_move_target_carveout_does_not_swallow_unrelated_false_claim():
    # Re1 is legal; prose claims a knight on e1 (wrong piece for the
    # move's target). Knight-on-e1 must still flag.
    board = chess.Board("4k3/8/8/8/8/8/8/5RK1 w - - 0 1")
    text = "Play Re1, then the knight on e1 covers d3."
    assert find_false_piece_claims(text, [board]) == ["knight on e1"]


def test_move_target_carveout_color_must_match_move():
    # Re1 is white's; claim says "black rook on e1" -- the carve-out
    # only covers white's rook (the move's color). Black-rook-on-e1
    # is still a false claim.
    board = chess.Board("4k3/8/8/8/8/8/8/5RK1 w - - 0 1")
    text = "Play Re1. Then black rook on e1 trades."
    assert find_false_piece_claims(text, [board]) == ["black rook on e1"]


def test_unreachable_square_without_legal_move_is_flagged():
    # No legal move lands a rook on e5 (king blocks the file), and no SAN
    # in prose; the false claim is flagged.
    board = chess.Board("4k3/8/8/8/8/8/8/5RK1 w - - 0 1")
    text = "The rook on e5 supports the file."
    assert find_false_piece_claims(text, [board]) == ["rook on e5"]


def test_move_target_carveout_illegal_san_does_not_trigger():
    # 'Re5' from this position is illegal for the f1 rook (not adjacent
    # rank, and a king blocks); the carve-out must not fire.
    board = chess.Board("4k3/8/8/8/8/8/8/5RK1 w - - 0 1")
    text = "Imagine Re5; then the rook on e5 dominates."
    out = find_false_piece_claims(text, [board])
    assert "rook on e5" in out


# ---------- Reachable-square carve-out (plain-English plans) ----------
# A piece-on-square named in prose, not SAN, clears when a legal move
# lands that piece/color on the square -- a forward-looking plan, however
# phrased. Unreachable squares still flag.


# Real model prose that tripped false negatives -- d3 is reachable by a
# legal black knight move, so the claim clears however it is phrased.
# Reuses _REVIEWED_FEN (the actual position reviewed).


def test_reachable_square_clears_however_phrased():
    board = chess.Board(_REVIEWED_FEN)
    for text in (
        "Black places the knight on d3, securing a formidable outpost that "
        "centralizes control.",
        "Black anchors the knight on d3 to restrict White's coordination "
        "and exert heavy pressure on the center.",
        "Black enjoys strong central pressure from the knight on d3.",
    ):
        assert find_false_piece_claims(text, [board]) == [], text


def test_reachable_via_capture_clears():
    # cxb5 lands a white pawn on b5; the claim reads as a reachable plan.
    board = chess.Board("r4bk1/2q2p2/4p2p/1pp1P2Q/b1PpP3/3P1NPP/6BK/5R2 w - - 0 26")
    assert find_false_piece_claims("White's b5 pawn is strong.", [board]) == []


def test_unreachable_target_still_flags():
    # No knight can legally reach a1, so the false claim is flagged.
    board = chess.Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    text = "White develops the knight on a1 for central control."
    # Color word is not adjacent to the piece (verb intervenes), so the
    # claim carries no color prefix.
    assert find_false_piece_claims(text, [board]) == ["knight on a1"]


# ---------- Bare-pawn-push carve-out (forward-looking prose) ----------
# Real case: model recommends a pawn push in bare-square form ("e4")
# and then describes the resulting pawn. Bare pawn pushes are out of
# scope for illegal-move detection (prose mentions squares constantly),
# but the move-target carve-out must still recognize a *legal* bare
# pawn push so post-move prose like "the e4 pawn" isn't flagged as a
# false claim against the live (pre-move) board.


def test_move_target_carveout_bare_pawn_push_white():
    board = chess.Board()
    text = "Play e4. The e4 pawn controls d5 and f5."
    assert find_false_piece_claims(text, [board]) == []


def test_move_target_carveout_bare_pawn_push_on_square_phrasing():
    board = chess.Board()
    text = "1.e4 is the main line; the pawn on e4 stakes the center."
    assert find_false_piece_claims(text, [board]) == []


def test_move_target_carveout_bare_pawn_push_color_must_match():
    # Carve-out covers the moving side only (mirrors SAN-move carve-out).
    board = chess.Board()
    text = "Play e4. Then black pawn on e4 -- wait, that's impossible."
    assert find_false_piece_claims(text, [board]) == ["black pawn on e4"]


def test_move_target_carveout_bare_pawn_push_wrong_piece_still_flags():
    # Pawn-push doesn't excuse a knight claim on the same square.
    board = chess.Board()
    text = "Play e4. The knight on e4 then jumps to f6."
    assert find_false_piece_claims(text, [board]) == ["knight on e4"]


def test_illegal_bare_pawn_push_does_not_create_carveout():
    # e5 is NOT legal for White from startpos (blocked by own pawn).
    # The carve-out must only fire on legal bare pawn pushes.
    board = chess.Board()
    text = "Imagine e5; then the pawn on e5 cramps Black."
    assert find_false_piece_claims(text, [board]) == ["pawn on e5"]


def _history_boards_after(moves_san: list[str]) -> list[chess.Board]:
    """Play `moves_san` from startpos and return [current, current.pop(),
    ..., startpos] -- the same sequence the coordinator builds for
    commentator-mode validation."""
    cur = chess.Board()
    for san in moves_san:
        cur.push_san(san)
    boards: list[chess.Board] = [cur.copy()]
    walker = cur.copy()
    while walker.move_stack:
        walker.pop()
        boards.append(walker.copy())
    return boards


def test_history_walk_accepts_piece_from_earlier_position():
    boards = _history_boards_after(["e4", "e5", "Nf3", "Nc6", "Bb5"])
    # Bb5 is on b5 now; on earlier boards the bishop was on f1.
    text = "The bishop on f1 developed to b5."
    assert find_false_piece_claims(text, boards) == []


def test_history_walk_flags_piece_present_nowhere():
    boards = _history_boards_after(["e4", "e5"])
    # No knight ever lived on f6 in this short opening.
    text = "The knight on f6 controls the center."
    assert find_false_piece_claims(text, boards) == ["knight on f6"]


def test_history_walk_does_not_excuse_king_on_old_square():
    # A king is unique and always present, so a king-on-square claim is
    # about the live position. After castling the king left e8; the walk
    # must NOT excuse "king on e8" just because it sat there at startpos.
    boards = _history_boards_after(
        ["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "O-O", "Nf6", "d3", "O-O"]
    )
    text = "The king on e8 is not under attack."
    assert find_false_piece_claims(text, boards) == ["king on e8"]


def test_history_walk_accepts_san_legal_in_an_earlier_position():
    boards = _history_boards_after(["e4", "e5", "Nf3"])
    # Nf3 was actually played at ply 3; reference to it from a later
    # position passes via the played-move match.
    text = "Then White played Nf3."
    assert find_illegal_moves(text, boards) == []


def test_history_walk_rejects_legal_but_never_played_alternative():
    # After 1.Nf3, the knight has moved off g1 -- Ng1 is illegal now.
    # At startpos (ply 0) "Nc3" was legal but not what White played.
    # The token must be flagged: not legal now, never played.
    boards = _history_boards_after(["Nf3", "Nf6", "Ng1"])
    text = "White could have tried Nc3 first."
    assert "Nc3" in find_illegal_moves(text, boards)


def test_history_walk_castle_word_accepted_if_legal_anywhere():
    # Walk an opening where castling is legal in an intermediate
    # position even if it ceases to be legal later (e.g. moving the
    # king). Here castling stays legal throughout, but the test pins
    # the multi-board acceptance.
    boards = _history_boards_after(["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5"])
    text = "White can castle shortly."
    assert find_castle_word_violations(text, boards) == []


# A square reused by a later piece (b2 pawn captured, queen now there)
# is the failure mode the `committed` flag fixes: pre-commit prose may
# still reference the departed pawn historically, but the closing
# post-recommendation plan must validate against the live board, where
# "capturing the pawn on b2" is false -- b2 holds the queen.
_IMMORTAL_TO_MOVE_19 = (
    "e4 e5 f4 exf4 Bc4 Qh4+ Kf1 b5 Bxb5 Nf6 Nf3 Qh6 d3 Nh5 Nh4 Qg5 "
    "Nf5 c6 g4 Nf6 Rg1 cxb5 h4 Qg6 h5 Qg5 Qf3 Ng8 Bxf4 Qf6 Nc3 Bc5 "
    "Nd5 Qxb2 Bd6 Qxa1+ Ke2"
).split()
_REUSED_SQUARE_CLAIM = "Capturing the pawn on b2 increases Black's advantage."


def _immortal_board() -> chess.Board:
    board = chess.Board()
    for san in _IMMORTAL_TO_MOVE_19:
        board.push_san(san)
    return board


def test_committed_collapses_history_walk_to_live_board():
    board = _immortal_board()
    pre = _boards_for_validation(board, "commentator", committed=False)
    post = _boards_for_validation(board, "commentator", committed=True)
    assert len(pre) > 1, "uncommitted commentator must keep the history walk"
    assert post == [board], "committed must collapse to the live board only"


def test_committed_flags_claim_on_square_reused_by_later_piece():
    board = _immortal_board()
    pre = _boards_for_validation(board, "commentator", committed=False)
    post = _boards_for_validation(board, "commentator", committed=True)
    # Pre-commit: the b2 pawn lived there for most of the game, so the
    # history walk excuses the claim. Post-commit: live board only, flagged.
    assert find_false_piece_claims(_REUSED_SQUARE_CLAIM, pre) == []
    assert find_false_piece_claims(_REUSED_SQUARE_CLAIM, post) == ["pawn on b2"]


def test_reachable_carveout_uses_live_board_not_oldest_in_walk():
    # Regression: the commentator walk is current-first (boards[0] is live,
    # pops append priors). The reachability carve-out must test the LIVE
    # board, not boards[-1] (the start), or a plan square reachable now but
    # not at the start gets falsely flagged. Live: black knight can reach d3.
    live = chess.Board(_REVIEWED_FEN)
    walk = [live, chess.Board(), chess.Board()]  # live first, older priors after
    assert find_false_piece_claims("The knight on d3 is a powerful anchor.", walk) == []


# --- extra_boards: positions the model examined via tool calls ---------
# A move/piece legal-or-present only in an examined (projected) position
# is legitimate forward-looking reasoning, not a live-board hallucination.
_PROJ_KNIGHT_ON_F4 = "rnbqkb1r/pppppppp/8/8/5N2/8/PPPPPPPP/RNBQKB1R w KQkq - 0 1"
_PROJ_KNIGHT_ON_D5 = "rnbqkb1r/pppppppp/8/3N4/8/8/PPPPPPPP/RNBQKB1R b KQkq - 1 1"


def test_illegal_move_flagged_without_extra_board():
    # Nd5 is illegal at the start; with no examined position it's flagged.
    assert find_illegal_moves("Play Nd5.", [chess.Board()]) == ["Nd5"]


def test_illegal_move_passes_when_legal_in_extra_board():
    # The model examined a position (knight on f4) where Nd5 is legal, so
    # naming it in prose is projected-line reasoning, not a hallucination.
    extra = [chess.Board(_PROJ_KNIGHT_ON_F4)]
    assert find_illegal_moves("Play Nd5.", [chess.Board()], extra) == []


def test_false_piece_claim_flagged_without_extra_board():
    assert find_false_piece_claims(
        "The knight on d5 dominates.", [chess.Board()],
    ) == ["knight on d5"]


def test_piece_claim_passes_when_present_in_extra_board():
    extra = [chess.Board(_PROJ_KNIGHT_ON_D5)]
    assert find_false_piece_claims(
        "The knight on d5 dominates.", [chess.Board()], extra,
    ) == []


# --- find_illegal_continuations: validate a run of moves AS a sequence ---
# Per-token validation accepts a move legal on any board; a continuation
# can pass that way while being an incoherent line. These pin the
# sequence semantics: a run is legal iff it plays from some anchor board.
def _board_after(moves_san: list[str]) -> chess.Board:
    board = chess.Board()
    for san in moves_san:
        board.push_san(san)
    return board


def test_continuation_legal_line_from_startpos_passes():
    text = "1.e4 e5 2.Nf3 Nc6 3.Bb5 a6"
    assert find_illegal_continuations(text, [chess.Board()]) == []


def test_continuation_bare_line_without_move_numbers_passes():
    # Bare pawn pushes are included inside a run; the run disambiguates
    # them from prose square references (unlike per-token detection).
    assert find_illegal_continuations("e4 e5 Nf3 Nc6 Bb5 a6", [chess.Board()]) == []


def test_continuation_incoherent_line_flagged():
    # Each move is individually legal on some board, but after 1.e4 e5
    # 2.Nf3 it is Black to move, so a second Nf3 cannot play -- not a
    # legal sequence even though every token is legal somewhere.
    text = "e4 e5 Nf3 Nf3"
    assert find_illegal_continuations(text, [chess.Board()]) == ["e4 e5 Nf3 Nf3"]


def test_continuation_per_token_validator_misses_what_sequence_catches():
    # Contrast: the per-token validator accepts the incoherent line because
    # each token is legal on some board. The sequence validator catches it.
    text = "e4 e5 Nf3 Nf3"
    assert find_illegal_moves(text, [chess.Board()]) == []
    assert find_illegal_continuations(text, [chess.Board()]) == ["e4 e5 Nf3 Nf3"]


def test_continuation_illegal_move_mid_line_flagged():
    # Bb5 is illegal after 1.e4 e5 2.Ke2 Ke7: the king on e2 blocks the
    # f1 bishop's diagonal, so it can't reach b5.
    text = "e4 e5 Ke2 Ke7 Bb5"
    assert find_illegal_continuations(text, [chess.Board()]) == ["e4 e5 Ke2 Ke7 Bb5"]


def test_continuation_prose_between_moves_breaks_the_run():
    # A word between two moves means it is description, not a line.
    text = "Nf3 is strong, and Bb5 too."
    assert find_illegal_continuations(text, [chess.Board()]) == []


def test_continuation_single_move_is_not_a_continuation():
    # A lone move is the per-token validator's job, not this one.
    assert find_illegal_continuations("Consider Nf3 here.", [chess.Board()]) == []


def test_continuation_dedup_repeated_run_listed_once():
    text = "The line e4 e5 Nf3 Nf3 fails; e4 e5 Nf3 Nf3 again."
    assert find_illegal_continuations(text, [chess.Board()]) == ["e4 e5 Nf3 Nf3"]


def test_continuation_anchored_to_a_non_start_board():
    # 3.Bb5 Nf6 is a legal line only after 1.e4 e5 2.Nf3 Nc6; when that
    # board is among the anchors it plays cleanly and is not flagged.
    mid = _board_after(["e4", "e5", "Nf3", "Nc6"])
    assert find_illegal_continuations("Bb5 Nf6", [chess.Board(), mid]) == []


def test_continuation_floating_quote_unanchored_is_left_alone():
    # Bb5 is illegal on the only anchor (startpos), so the run cannot be
    # placed -- it is a quoted line continuing from context we don't have.
    # Flagging it would false-positive on legitimate mid-line quotes.
    assert find_illegal_continuations("Bb5 Nf6", [chess.Board()]) == []


def test_continuation_anchored_to_an_extra_board():
    # The line is legal only from an examined (projected) position.
    mid = _board_after(["e4", "e5", "Nf3", "Nc6"])
    assert find_illegal_continuations("Bb5 Nf6", [chess.Board()], [mid]) == []


def test_continuation_no_anchor_boards_returns_empty():
    # No board to validate against -> nothing flagged (matches the
    # coordinator short-circuit when no live board is available).
    assert find_illegal_continuations("e4 e5 Nf3 Nf3", []) == []


def test_continuation_white_numbered_line_on_black_anchor_not_flagged():
    # Black-to-move anchor; the quoted line is "20.e5 ..." -- white's move,
    # a ply ahead. Numbered white, so not replayable here and not flagged.
    board = chess.Board(
        "rnb1k1nr/p2p1ppp/3B4/1pbN1N1P/4P1P1/3P1Q2/P1P1K3/q5R1 b kq - 1 19"
    )
    text = "20.e5 Na6 21.Nxg7+ Kd8 22.Qf6+ Nxf6 23.Be7#"
    assert find_illegal_continuations(text, [board]) == []


# --- prose-vs-line discrimination: descriptive prose must not be flagged ---
# A run of bare squares in commentary ("doubled c3 c4 pawns") is not a
# move line. Two guards prevent false positives: a line-proof token must
# be present, and the first move must be legal on some anchor.
@pytest.mark.parametrize("prose", [
    "f7 g6 h6 are weak squares",
    "doubled c3 c4 pawns",
    "the a7 b6 c5 pawn wedge",
    "rooks on a1 d1 dominate",
    "his f5 e6 pawns cramp us",
    "the e4 d5 pawn structure",
])
def test_continuation_bare_square_prose_not_flagged(prose):
    assert find_illegal_continuations(prose, [chess.Board()]) == []


def test_continuation_two_separate_lines_in_prose_not_misanchored():
    # The second run ("Bb5 a6") legitimately continues from after the
    # first; validated from startpos in isolation it would falsely flag.
    # The first-move-anchor guard leaves the unanchorable run alone.
    text = "Play Nf3 Nc6. Then Bb5 a6."
    assert find_illegal_continuations(text, [chess.Board()]) == []


def test_continuation_white_only_numbered_shorthand_skipped():
    # "1.e4 2.Nf3" omits Black's plies (each move carries its own number),
    # so it is not a ply sequence to replay -- skip rather than flag.
    assert find_illegal_continuations("1.e4 2.Nf3", [chess.Board()]) == []


def test_continuation_report_strips_move_numbers():
    # The flagged string is the clean move list, not the raw run -- so
    # leading "1."/"2." prefixes don't leak into the corrective message.
    text = "1. e4 e5 2. Nf3 Nf3"
    assert find_illegal_continuations(text, [chess.Board()]) == ["e4 e5 Nf3 Nf3"]


# --- the common path: a line continuing from the live (non-start) board ---
# A model usually quotes a line "from here". The first move is legal on the
# live board, so the run anchors there and the whole sequence is replayed.
_LIVE_NIMZO = ["d4", "Nf6", "c4", "e6", "Nc3"]  # Black to move


def test_continuation_break_in_live_board_line_flagged():
    # From the live position a piece-move line that breaks mid-sequence
    # (a second Nf3 with no reply between) must be flagged.
    live = _board_after(_LIVE_NIMZO)
    assert find_illegal_continuations("Bb4 Nf3 Nf3", [live]) == ["Bb4 Nf3 Nf3"]


def test_continuation_clean_live_board_line_passes():
    # A legal line from the live position plays cleanly -> not flagged.
    live = _board_after(_LIVE_NIMZO)
    assert find_illegal_continuations("Bb4 Nf3 d5 cxd5 exd5", [live]) == []


def test_continuation_legal_from_earlier_history_board_passes():
    # Commentator anchors are [live, ...prior, startpos]. A line quoted
    # from an earlier position (legal from startpos, not from live) is not
    # flagged: it anchors on the board where its first move is legal.
    live = _board_after(_LIVE_NIMZO)
    boards = [live, chess.Board()]
    assert find_illegal_continuations("e4 e5 Nf3 Nc6", boards) == []


def test_continuation_bare_pawn_only_broken_line_is_not_flagged():
    # Accepted tradeoff: "e4 e4" is broken (Black can't push e4 after
    # 1.e4) but a bare-pawn-only run has no line-proof token, so it is
    # skipped as prose -- the same gate that passes "doubled c3 c4 pawns".
    assert find_illegal_continuations("e4 e4", [chess.Board()]) == []


# ---------- Move-attribution / wrong side-to-move ---------------------
# Prose that credits a move to the wrong side ("White plays <Black's
# move>"). Flagged only when the named color != side to move AND the SAN
# is the side-to-move's legal move, not the named side's. Reuses
# _REVIEWED_FEN (black to move).


def test_attribution_wrong_side_flagged():
    # Black to move; "White plays Nd3" credits Black's move to White.
    board = chess.Board(_REVIEWED_FEN)
    out = find_move_attribution_errors("White plays Nd3, seizing the center.", [board])
    assert out == ["White Nd3"]


def test_attribution_correct_side_not_flagged():
    board = chess.Board(_REVIEWED_FEN)
    assert find_move_attribution_errors("Black plays Nd3, a strong outpost.", [board]) == []


def test_attribution_possessive_not_flagged():
    # "White's Nd3" is ambiguous with a piece reference ("the knight on
    # d3"); the attribution validator does not match possessives -- a wrong
    # piece claim there is find_false_piece_claims's job, not this one.
    board = chess.Board(_REVIEWED_FEN)
    assert find_move_attribution_errors("White's Nd3 dominates the board.", [board]) == []


def test_attribution_no_color_word_not_flagged():
    board = chess.Board(_REVIEWED_FEN)
    assert find_move_attribution_errors("Nd3 is a strong outpost.", [board]) == []


def test_attribution_piece_on_square_not_a_move():
    # "White bishop on g5" is a piece reference, not a move -- no SAN, no flag.
    board = chess.Board(_REVIEWED_FEN)
    assert find_move_attribution_errors("White bishop on g5 pins the knight.", [board]) == []


def test_attribution_move_not_legal_for_either_owner_not_flagged():
    # "White plays Rd4" -- not a legal Black move (side to move), so it is
    # not a wrong-attribution of the STM's move; left to other validators.
    board = chess.Board(_REVIEWED_FEN)
    assert find_move_attribution_errors("White plays Rd4.", [board]) == []


def test_attribution_skipped_when_side_to_move_in_check():
    # Black to move and in check: flipping turn to test the named side would
    # be an illegal position, so attribution bails rather than guess.
    board = chess.Board("4k3/4Q3/8/8/8/8/8/4K3 b - - 0 1")
    assert board.is_check()
    assert find_move_attribution_errors("White plays Qe8 next.", [board]) == []
