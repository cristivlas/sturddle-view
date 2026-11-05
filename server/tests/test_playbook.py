"""Playbook: situation classifier (play/playbook.py, play/matchers.py) and
fragment rendering (llm/playbook.py). Positions are built to sit on one side of each
threshold; text assertions check fragment identity, never exact bytes.
"""
from __future__ import annotations

import chess
import pytest

import sturddle_view.llm.playbook as playbook_prompts
from sturddle_view.llm.playbook import render_playbook, structural_playbook
from sturddle_view.llm.prompts import COACH_MODE, COMMENTATOR_MODE, PLAYBOOK_LEAD
from sturddle_view.play.matchers import (
    castling_tag,
    imbalance_tag,
    structure_tag,
)
from sturddle_view.play.playbook import (
    DECISIVE_CP,
    EDGE_CP,
    RESIGN_CP,
    Situation,
    classify,
    phase_tag,
    repeating_moves,
)
from sturddle_view.play.playbook_catalog import (
    NOTE_CRUSHING,
    NOTE_LOST,
    NOTE_REPEAT_AHEAD,
    NOTE_REPEAT_BEHIND,
    SACRIFICE_NOTES,
    SHIPPED_DIR,
    load_catalog,
)
from sturddle_view.play.playbook_tags import (
    AXIS_BISHOPS,
    AXIS_CASTLING,
    AXIS_IMBALANCE,
    AXIS_MARGIN,
    AXIS_PAWN,
    AXIS_PHASE,
    AXIS_STRUCTURE,
    BISHOPS_OPPOSITE,
    BISHOPS_OPPOSITE_QUEENS,
    CASTLING_OPPOSITE,
    IMBALANCE_IQP_OURS,
    IMBALANCE_IQP_THEIRS,
    IMBALANCE_MINORITY_OURS,
    IMBALANCE_MINORITY_THEIRS,
    MARGIN_BETTER,
    MARGIN_CRUSHING,
    MARGIN_EVEN,
    MARGIN_LOSING,
    MARGIN_LOST,
    MARGIN_WINNING,
    MARGIN_WORSE,
    PAWN_PASSED_OUTSIDE_OURS,
    PAWN_PASSED_OUTSIDE_THEIRS,
    PHASE_ENDGAME,
    PHASE_LATE,
    PHASE_MIDDLEGAME,
    PHASE_OPENING,
    SOURCE_EVAL,
    SOURCE_MATERIAL,
    STRUCTURE_CLOSED,
    STRUCTURE_OPEN,
)
from sturddle_view.play.sacrifices import (
    PATTERN_BISHOP_F7,
    PATTERN_GREEK_GIFT,
    PATTERN_KING_ZONE,
)


# 16 pawns, three locked pairs (d4/d5, e5/e6, f4/f5), no open file.
_CLOSED_FEN = "k7/ppp3pp/4p3/3pPp2/3P1P2/2P5/PP4PP/K7 w - - 0 1"
# 8 pawns on the a, b, g, h files: four open files.
_OPEN_FEN = "k7/pp4pp/8/8/8/8/PP4PP/K7 w - - 0 1"
# 12 pawns, no locks, two open files: neither structure.
_MIDDLEGAME_FEN = "k7/pppppp2/8/8/8/8/PPPPPP2/K7 w - - 0 1"
# 4 pawns.
_ENDGAME_FEN = "k7/pp6/8/8/8/8/PP6/K7 w - - 0 1"
_BARE_KINGS_FEN = "8/5k2/8/8/8/8/3K4/8 w - - 0 1"
# White is a rook up by material.
_ROOK_UP_FEN = "k7/8/8/8/8/8/8/K3R3 w - - 0 1"

# White IQP on d4 against e6; 12 pawns.
_IQP_FEN = "6k1/pp3ppp/4p3/8/3P4/8/PP3PPP/6K1 w - - 0 1"
_IQP_OWN_C_PAWN_FEN = "6k1/pp3ppp/4p3/8/3P4/8/PPP2PPP/6K1 w - - 0 1"
# No e6: the lone d-pawn is passed.
_IQP_PASSED_FEN = "6k1/pp3ppp/8/8/3P4/8/PP3PPP/6K1 w - - 0 1"
# d4 vs e6 with 2 pawns: the endgame bucket.
_IQP_ENDGAME_FEN = "6k1/8/4p3/8/3P4/8/8/6K1 w - - 0 1"
# Carlsbad: White a+b vs a+b+c, d4/d5 locked, 7 pawns each.
_MINORITY_FEN = "6k1/pp3ppp/2p5/3p4/3P4/4P3/PP3PPP/6K1 w - - 0 1"
_MINORITY_UNEQUAL_FEN = "6k1/pp3pp1/2p5/3p4/3P4/4P3/PP3PPP/6K1 w - - 0 1"
# Same pawn counts, but a d5 / d6 lock: a Benoni, not a Carlsbad.
_MINORITY_BENONI_FEN = "6k1/pp3ppp/3p4/2pP4/4P3/8/PP3PPP/6K1 w - - 0 1"
# Dark c1 bishop vs light c8 bishop.
_OPP_BISHOPS_QUEENS_FEN = "2bq2k1/pp3ppp/8/8/8/8/PP3PPP/2BQ2K1 w - - 0 1"
_OPP_BISHOPS_NO_BLACK_QUEEN_FEN = "2b3k1/pp3ppp/8/8/8/8/PP3PPP/2BQ2K1 w - - 0 1"
_OPP_BISHOPS_KNIGHT_FEN = "2bq2k1/pp3ppp/8/8/8/8/PP3PPP/1NBQ2K1 w - - 0 1"
_SAME_BISHOPS_FEN = "3q1bk1/pp3ppp/8/8/8/8/PP3PPP/2BQ2K1 w - - 0 1"
_OPP_BISHOPS_ENDGAME_FEN = "2b3k1/p6p/8/8/8/8/P6P/2B3K1 w - - 0 1"
# White a4 passer; 5 pawns.
_OUTSIDE_PASSER_FEN = "4k3/5pp1/8/8/P7/8/5PP1/4K3 w - - 0 1"
_OUTSIDE_PASSER_ENDGAME_FEN = "4k3/5p2/8/8/P7/8/5P2/4K3 w - - 0 1"
_D_FILE_PASSER_FEN = "4k3/5pp1/8/8/3P4/8/5PP1/4K3 w - - 0 1"
# White a4 and black h5 passers.
_BOTH_PASSERS_FEN = "4k3/5p2/8/7p/P7/8/5P2/4K3 w - - 0 1"
# Kc1 vs Kg8, queens on; 12 pawns.
_OPP_CASTLING_FEN = "3q2k1/ppp2ppp/8/8/8/8/PPP2PPP/2KQ4 w - - 0 1"
_SAME_WING_KINGS_FEN = "3q2k1/ppp2ppp/8/8/8/8/PPP2PPP/3Q2K1 w - - 0 1"
_OPP_CASTLING_NO_BLACK_QUEEN_FEN = "6k1/ppp2ppp/8/8/8/8/PPP2PPP/2KQ4 w - - 0 1"
_OPP_CASTLING_ENDGAME_FEN = "3q2k1/p6p/8/8/8/8/P6P/2KQ4 w - - 0 1"
# Bd3, Nf3, Qd1 against a king on g8, g5 safe: the Greek gift Bxh7+.
_GREEK_GIFT_FEN = "r1b2rk1/ppqn1ppp/2n1p3/2bpP3/8/2PB1N2/PP1N1PPP/R1BQ1RK1 w - - 0 1"
_GREEK_GIFT = ((PATTERN_GREEK_GIFT, "Bxh7+"),)


# Shipped text only: assertions name the fragment a tag selects.
_SHIPPED = load_catalog(SHIPPED_DIR, None)


def _frag(axis: str, tag: str) -> str:
    return _SHIPPED.fragment(axis, tag)


def _situation(**overrides) -> Situation:
    base = dict(
        margin=MARGIN_EVEN, margin_source=SOURCE_EVAL,
        phase=PHASE_MIDDLEGAME, structure=None,
    )
    base.update(overrides)
    return Situation(**base)


def _shuffled_knights() -> chess.Board:
    """1.Nf3 Nf6 2.Ng1 Ng8: 3.Nf3 repeats the position after 1.Nf3."""
    board = chess.Board()
    for uci in ("g1f3", "g8f6", "f3g1", "f6g8"):
        board.push_uci(uci)
    return board


# --- classifier ------------------------------------------------------------

@pytest.mark.parametrize("cp, expected", [
    (-RESIGN_CP, MARGIN_LOST),
    (-DECISIVE_CP, MARGIN_LOSING),
    (-EDGE_CP, MARGIN_WORSE),
    (-EDGE_CP + 1, MARGIN_EVEN),
    (0, MARGIN_EVEN),
    (EDGE_CP - 1, MARGIN_EVEN),
    (EDGE_CP, MARGIN_BETTER),
    (DECISIVE_CP, MARGIN_WINNING),
    (RESIGN_CP, MARGIN_CRUSHING),
])
def test_margin_buckets_white_pov(cp, expected):
    got = classify(chess.Board(), score_white={"cp": cp}, our_color=chess.WHITE)
    assert got.margin == expected
    assert got.margin_source == SOURCE_EVAL


def test_margin_flips_for_black():
    got = classify(chess.Board(), score_white={"cp": DECISIVE_CP}, our_color=chess.BLACK)
    assert got.margin == MARGIN_LOSING


@pytest.mark.parametrize("mate, color, expected", [
    (3, chess.WHITE, MARGIN_CRUSHING),
    (3, chess.BLACK, MARGIN_LOST),
    (-2, chess.WHITE, MARGIN_LOST),
    (-2, chess.BLACK, MARGIN_CRUSHING),
])
def test_mate_scores_map_to_extremes(mate, color, expected):
    got = classify(chess.Board(), score_white={"mate": mate}, our_color=color)
    assert got.margin == expected


def test_material_fallback_when_no_eval():
    got = classify(chess.Board(_ROOK_UP_FEN), score_white=None, our_color=chess.WHITE)
    assert got.margin == MARGIN_WINNING
    assert got.margin_source == SOURCE_MATERIAL
    got = classify(chess.Board(_ROOK_UP_FEN), score_white=None, our_color=chess.BLACK)
    assert got.margin == MARGIN_LOSING


def test_unusable_eval_dicts_fall_back_to_material():
    for score in ({}, {"mate": 0}, {"cp": "x"}):
        got = classify(chess.Board(), score_white=score, our_color=chess.WHITE)
        assert got.margin == MARGIN_EVEN
        assert got.margin_source == SOURCE_MATERIAL


@pytest.mark.parametrize("fen, expected", [
    (chess.STARTING_FEN, PHASE_OPENING),
    (_CLOSED_FEN, PHASE_OPENING),
    (_MIDDLEGAME_FEN, PHASE_MIDDLEGAME),
    (_OPEN_FEN, PHASE_LATE),
    (_ENDGAME_FEN, PHASE_ENDGAME),
    (_BARE_KINGS_FEN, PHASE_ENDGAME),
])
def test_phase_by_pawn_buckets(fen, expected):
    assert phase_tag(chess.Board(fen)) == expected


@pytest.mark.parametrize("fen, expected", [
    (chess.STARTING_FEN, None),
    (_CLOSED_FEN, STRUCTURE_CLOSED),
    (_OPEN_FEN, STRUCTURE_OPEN),
    (_MIDDLEGAME_FEN, None),
])
def test_structure_tag(fen, expected):
    assert structure_tag(chess.Board(fen)) == expected


def test_repeating_moves_need_the_move_stack():
    assert repeating_moves(chess.Board()) == ()
    assert repeating_moves(chess.Board(_shuffled_knights().fen())) == ()


def test_repeating_moves_lists_the_repeating_san():
    board = _shuffled_knights()
    assert repeating_moves(board) == ("Nf3",)
    # The caller's board is left untouched.
    assert len(board.move_stack) == 4
    got = classify(board, score_white=None, our_color=chess.WHITE)
    assert got.repeats == ("Nf3",)


def test_classify_lists_the_side_to_move_sacrifices():
    board = chess.Board(_GREEK_GIFT_FEN)
    assert classify(board, score_white=None, our_color=chess.WHITE).sacrifices == _GREEK_GIFT
    # Our side is not on move: the margin is not the mover's, so none.
    assert classify(board, score_white=None, our_color=chess.BLACK).sacrifices == ()


def _sacrifice_note(pattern: str, move: str) -> str:
    return _SHIPPED.note(SACRIFICE_NOTES[pattern], move=move)


def test_every_pattern_has_a_shipped_note():
    assert set(SACRIFICE_NOTES) == {PATTERN_GREEK_GIFT, PATTERN_BISHOP_F7, PATTERN_KING_ZONE}
    for pattern in SACRIFICE_NOTES:
        assert "Bxh7+" in _sacrifice_note(pattern, "Bxh7+")


@pytest.mark.parametrize("margin", [MARGIN_EVEN, MARGIN_WORSE, MARGIN_LOSING, MARGIN_LOST])
def test_render_sacrifice_note_outside_the_ahead_margins(margin):
    line = render_playbook(
        _situation(margin=margin, sacrifices=_GREEK_GIFT), COACH_MODE, chess.WHITE,
    )
    assert _sacrifice_note(PATTERN_GREEK_GIFT, "Bxh7+") in line


@pytest.mark.parametrize("margin", [MARGIN_BETTER, MARGIN_WINNING, MARGIN_CRUSHING])
def test_no_sacrifice_note_ahead(margin):
    situation = _situation(margin=margin, sacrifices=_GREEK_GIFT)
    assert "Bxh7+" not in render_playbook(situation, COACH_MODE, chess.WHITE)
    assert structural_playbook(situation) is None


def test_render_sacrifice_note_rides_outside_the_cap(monkeypatch):
    monkeypatch.setattr(playbook_prompts, "MAX_FRAGMENTS", 1)
    line = render_playbook(
        _situation(margin=MARGIN_WORSE, structure=STRUCTURE_OPEN, sacrifices=_GREEK_GIFT),
        COACH_MODE, chess.WHITE,
    )
    assert _frag(AXIS_MARGIN, MARGIN_WORSE) in line
    assert _sacrifice_note(PATTERN_GREEK_GIFT, "Bxh7+") in line


def test_structural_playbook_carries_the_sacrifice_note():
    note = _sacrifice_note(PATTERN_GREEK_GIFT, "Bxh7+")
    alone = structural_playbook(_situation(sacrifices=_GREEK_GIFT))
    assert alone == PLAYBOOK_LEAD + note
    with_tag = structural_playbook(_situation(imbalance=IMBALANCE_IQP_OURS, sacrifices=_GREEK_GIFT))
    assert with_tag == PLAYBOOK_LEAD + _frag(AXIS_IMBALANCE, IMBALANCE_IQP_OURS) + " " + note


def test_structure_never_tagged_in_endgame():
    # Every file is open with bare kings; the bucket suppresses the tag.
    assert structure_tag(chess.Board(_BARE_KINGS_FEN)) == STRUCTURE_OPEN
    got = classify(chess.Board(_BARE_KINGS_FEN), score_white=None, our_color=chess.WHITE)
    assert got.phase == PHASE_ENDGAME
    assert got.structure is None


def _tags(board: chess.Board, our_color: chess.Color) -> Situation:
    return classify(board, score_white=None, our_color=our_color)


@pytest.mark.parametrize("fen, field, ours, theirs", [
    (_IQP_FEN, "imbalance", IMBALANCE_IQP_OURS, IMBALANCE_IQP_THEIRS),
    (_MINORITY_FEN, "imbalance", IMBALANCE_MINORITY_OURS, IMBALANCE_MINORITY_THEIRS),
    (_OUTSIDE_PASSER_FEN, "pawn", PAWN_PASSED_OUTSIDE_OURS, PAWN_PASSED_OUTSIDE_THEIRS),
])
def test_directional_tags_follow_our_color(fen, field, ours, theirs):
    # White owns the feature in `fen`; the mirror hands it to Black.
    board = chess.Board(fen)
    mirrored = board.mirror()
    assert getattr(_tags(board, chess.WHITE), field) == ours
    assert getattr(_tags(board, chess.BLACK), field) == theirs
    assert getattr(_tags(mirrored, chess.BLACK), field) == ours
    assert getattr(_tags(mirrored, chess.WHITE), field) == theirs


@pytest.mark.parametrize("fen, field, expected", [
    (_OPP_BISHOPS_QUEENS_FEN, "bishops", BISHOPS_OPPOSITE_QUEENS),
    (_OPP_BISHOPS_NO_BLACK_QUEEN_FEN, "bishops", BISHOPS_OPPOSITE),
    (_OPP_BISHOPS_ENDGAME_FEN, "bishops", BISHOPS_OPPOSITE),
    (_OPP_CASTLING_FEN, "castling", CASTLING_OPPOSITE),
])
def test_symmetric_tags_hold_for_both_colors(fen, field, expected):
    board = chess.Board(fen)
    for b in (board, board.mirror()):
        for color in chess.COLORS:
            assert getattr(_tags(b, color), field) == expected


@pytest.mark.parametrize("fen, field", [
    (_IQP_OWN_C_PAWN_FEN, "imbalance"),
    (_IQP_PASSED_FEN, "imbalance"),
    (_MINORITY_UNEQUAL_FEN, "imbalance"),
    (_MINORITY_BENONI_FEN, "imbalance"),
    (_OPP_BISHOPS_KNIGHT_FEN, "bishops"),
    (_SAME_BISHOPS_FEN, "bishops"),
    (_D_FILE_PASSER_FEN, "pawn"),
    (_BOTH_PASSERS_FEN, "pawn"),
    (_SAME_WING_KINGS_FEN, "castling"),
    (_OPP_CASTLING_NO_BLACK_QUEEN_FEN, "castling"),
])
def test_near_misses_are_untagged(fen, field):
    assert getattr(_tags(chess.Board(fen), chess.WHITE), field) is None


def test_imbalance_and_castling_never_tagged_in_endgame():
    # The predicates hold; the endgame bucket suppresses the tags.
    iqp = chess.Board(_IQP_ENDGAME_FEN)
    assert imbalance_tag(iqp, chess.WHITE) == IMBALANCE_IQP_OURS
    got = _tags(iqp, chess.WHITE)
    assert got.phase == PHASE_ENDGAME
    assert got.imbalance is None
    kings = chess.Board(_OPP_CASTLING_ENDGAME_FEN)
    assert castling_tag(kings) == CASTLING_OPPOSITE
    got = _tags(kings, chess.WHITE)
    assert got.phase == PHASE_ENDGAME
    assert got.castling is None


def test_pawn_and_bishops_tag_in_endgame():
    passer = _tags(chess.Board(_OUTSIDE_PASSER_ENDGAME_FEN), chess.WHITE)
    assert passer.phase == PHASE_ENDGAME
    assert passer.pawn == PAWN_PASSED_OUTSIDE_OURS
    bishops = _tags(chess.Board(_OPP_BISHOPS_ENDGAME_FEN), chess.WHITE)
    assert bishops.phase == PHASE_ENDGAME
    assert bishops.bishops == BISHOPS_OPPOSITE


# --- rendering -------------------------------------------------------------

def test_render_additive_margin_then_phase_then_structure():
    line = render_playbook(
        _situation(margin=MARGIN_WORSE, phase=PHASE_MIDDLEGAME, structure=STRUCTURE_OPEN),
        COACH_MODE, chess.WHITE,
    )
    assert line.startswith(PLAYBOOK_LEAD)
    assert "you are slightly worse;" in line
    margin_at = line.index(_frag(AXIS_MARGIN, MARGIN_WORSE))
    phase_at = line.index(_frag(AXIS_PHASE, PHASE_MIDDLEGAME))
    structure_at = line.index(_frag(AXIS_STRUCTURE, STRUCTURE_OPEN))
    assert margin_at < phase_at < structure_at


def test_render_phase_leads_in_endgame():
    line = render_playbook(
        _situation(margin=MARGIN_BETTER, phase=PHASE_ENDGAME), COACH_MODE, chess.WHITE,
    )
    phase_at = line.index(_frag(AXIS_PHASE, PHASE_ENDGAME))
    assert phase_at < line.index(_frag(AXIS_MARGIN, MARGIN_BETTER))


def test_render_even_margin_has_no_margin_fragment():
    line = render_playbook(_situation(), COACH_MODE, chess.WHITE)
    assert "you are level;" in line
    assert _frag(AXIS_PHASE, PHASE_MIDDLEGAME) in line
    assert not any(
        _frag(AXIS_MARGIN, m) in line
        for m in (MARGIN_WORSE, MARGIN_BETTER, MARGIN_LOSING, MARGIN_WINNING)
    )


def test_render_combo_replaces_margin_and_phase_keeps_structure():
    line = render_playbook(
        _situation(margin=MARGIN_WINNING, phase=PHASE_ENDGAME, structure=STRUCTURE_OPEN),
        COACH_MODE, chess.WHITE,
    )
    assert _SHIPPED.combo(MARGIN_WINNING, AXIS_PHASE, PHASE_ENDGAME) in line
    assert _frag(AXIS_MARGIN, MARGIN_WINNING) not in line
    assert _frag(AXIS_PHASE, PHASE_ENDGAME) not in line
    assert _frag(AXIS_STRUCTURE, STRUCTURE_OPEN) in line


def test_render_structure_combo_keeps_phase():
    line = render_playbook(
        _situation(margin=MARGIN_LOSING, phase=PHASE_MIDDLEGAME, structure=STRUCTURE_CLOSED),
        COACH_MODE, chess.WHITE,
    )
    assert _SHIPPED.combo(MARGIN_LOSING, AXIS_STRUCTURE, STRUCTURE_CLOSED) in line
    assert _frag(AXIS_STRUCTURE, STRUCTURE_CLOSED) not in line
    assert _frag(AXIS_PHASE, PHASE_MIDDLEGAME) in line


def test_render_extreme_margins_use_base_combo_plus_note():
    lost = render_playbook(
        _situation(margin=MARGIN_LOST, structure=STRUCTURE_OPEN), COACH_MODE, chess.WHITE,
    )
    assert _SHIPPED.combo(MARGIN_LOSING, AXIS_STRUCTURE, STRUCTURE_OPEN) in lost
    assert _SHIPPED.text(NOTE_LOST) in lost
    crushing = render_playbook(
        _situation(margin=MARGIN_CRUSHING, phase=PHASE_ENDGAME), COMMENTATOR_MODE, chess.BLACK,
    )
    assert _SHIPPED.combo(MARGIN_WINNING, AXIS_PHASE, PHASE_ENDGAME) in crushing
    assert _SHIPPED.text(NOTE_CRUSHING) in crushing


@pytest.mark.parametrize("margin, base", [
    (MARGIN_WINNING, MARGIN_WINNING),
    (MARGIN_CRUSHING, MARGIN_WINNING),
    (MARGIN_LOSING, MARGIN_LOSING),
    (MARGIN_LOST, MARGIN_LOSING),
])
def test_render_opposite_bishops_combo_outranks_the_endgame_combo(margin, base):
    # Ahead, the endgame combo says "trade pieces"; with opposite-colored
    # bishops that draws, so the bishops combo replaces it and the phase.
    line = render_playbook(
        _situation(margin=margin, phase=PHASE_ENDGAME, bishops=BISHOPS_OPPOSITE),
        COACH_MODE, chess.WHITE,
    )
    assert _SHIPPED.combo(base, AXIS_BISHOPS, BISHOPS_OPPOSITE) in line
    assert _SHIPPED.combo(base, AXIS_PHASE, PHASE_ENDGAME) not in line
    assert _frag(AXIS_PHASE, PHASE_ENDGAME) not in line
    assert _frag(AXIS_BISHOPS, BISHOPS_OPPOSITE) not in line


def test_render_opposite_bishops_combo_keeps_the_other_specific_tags():
    line = render_playbook(
        _situation(
            margin=MARGIN_WINNING, phase=PHASE_LATE, bishops=BISHOPS_OPPOSITE,
            pawn=PAWN_PASSED_OUTSIDE_OURS,
        ),
        COACH_MODE, chess.WHITE,
    )
    combo_at = line.index(_SHIPPED.combo(MARGIN_WINNING, AXIS_BISHOPS, BISHOPS_OPPOSITE))
    assert combo_at < line.index(_frag(AXIS_PAWN, PAWN_PASSED_OUTSIDE_OURS))


def test_render_opposite_bishops_combo_outranks_the_structure_combo():
    # winning + open alone says "simplification is the win": a draw here.
    line = render_playbook(
        _situation(
            margin=MARGIN_WINNING, phase=PHASE_LATE, structure=STRUCTURE_OPEN,
            bishops=BISHOPS_OPPOSITE,
        ),
        COACH_MODE, chess.WHITE,
    )
    assert _SHIPPED.combo(MARGIN_WINNING, AXIS_BISHOPS, BISHOPS_OPPOSITE) in line
    assert _SHIPPED.combo(MARGIN_WINNING, AXIS_STRUCTURE, STRUCTURE_OPEN) not in line
    # The tag rules out knights: "bishops over knights" would be moot.
    assert _frag(AXIS_STRUCTURE, STRUCTURE_OPEN) not in line


@pytest.mark.parametrize("margin, bishops", [
    (MARGIN_BETTER, BISHOPS_OPPOSITE),          # no combo for a small edge
    (MARGIN_WINNING, BISHOPS_OPPOSITE_QUEENS),  # queens on: attack, not trades
])
def test_render_opposite_bishops_without_a_combo_keeps_the_bishops_fragment(margin, bishops):
    line = render_playbook(
        _situation(margin=margin, phase=PHASE_ENDGAME, bishops=bishops), COACH_MODE, chess.WHITE,
    )
    assert _frag(AXIS_BISHOPS, bishops) in line


def test_structural_playbook_only_on_a_specific_tag():
    # Open / closed is threshold-tuned and generic: not a match.
    assert structural_playbook(_situation()) is None
    assert structural_playbook(_situation(structure=STRUCTURE_OPEN)) is None
    line = structural_playbook(_situation(imbalance=IMBALANCE_IQP_OURS))
    assert line == PLAYBOOK_LEAD + _frag(AXIS_IMBALANCE, IMBALANCE_IQP_OURS)


def test_structural_playbook_hands_over_no_verdict():
    # Behind with opposite-colored bishops and a repetition on the board:
    # the standing, margin combo and repetition note stay behind the tool.
    situation = _situation(
        margin=MARGIN_LOSING, phase=PHASE_ENDGAME, bishops=BISHOPS_OPPOSITE, repeats=("Nf3",),
    )
    line = structural_playbook(situation)
    assert line == PLAYBOOK_LEAD + _frag(AXIS_BISHOPS, BISHOPS_OPPOSITE)
    full = render_playbook(situation, COACH_MODE, chess.WHITE)
    assert _SHIPPED.combo(MARGIN_LOSING, AXIS_BISHOPS, BISHOPS_OPPOSITE) in full


def test_lost_note_is_coach_only_and_not_in_opening():
    lost = _situation(margin=MARGIN_LOST)
    assert _SHIPPED.text(NOTE_LOST) not in render_playbook(lost, COMMENTATOR_MODE, chess.WHITE)
    assert _SHIPPED.text(NOTE_LOST) not in render_playbook(
        _situation(margin=MARGIN_LOST, phase=PHASE_OPENING), COACH_MODE, chess.WHITE,
    )


def test_render_commentator_names_side_to_move():
    line = render_playbook(_situation(margin=MARGIN_BETTER), COMMENTATOR_MODE, chess.BLACK)
    assert "Black is slightly better;" in line
    assert "you" not in line.split(";")[0]


def test_render_material_source_is_hedged():
    line = render_playbook(
        _situation(margin=MARGIN_WINNING, margin_source=SOURCE_MATERIAL),
        COMMENTATOR_MODE, chess.WHITE,
    )
    assert "White is clearly better by material;" in line


def test_render_caps_fragment_count(monkeypatch):
    monkeypatch.setattr(playbook_prompts, "MAX_FRAGMENTS", 1)
    line = render_playbook(
        _situation(margin=MARGIN_WORSE, structure=STRUCTURE_OPEN), COACH_MODE, chess.WHITE,
    )
    assert _frag(AXIS_MARGIN, MARGIN_WORSE) in line
    assert _frag(AXIS_PHASE, PHASE_MIDDLEGAME) not in line
    assert _frag(AXIS_STRUCTURE, STRUCTURE_OPEN) not in line


def test_render_is_one_line():
    line = render_playbook(
        _situation(margin=MARGIN_LOST, structure=STRUCTURE_CLOSED, repeats=("Nf3",)),
        COACH_MODE, chess.WHITE,
    )
    assert "\n" not in line


def test_render_repetition_note_by_margin():
    repeats = ("Nf3", "Nh3")
    behind = render_playbook(
        _situation(margin=MARGIN_LOSING, repeats=repeats), COACH_MODE, chess.WHITE,
    )
    assert behind.endswith(_SHIPPED.note(NOTE_REPEAT_BEHIND, moves="Nf3, Nh3"))
    ahead = render_playbook(
        _situation(margin=MARGIN_BETTER, repeats=repeats), COACH_MODE, chess.WHITE,
    )
    assert ahead.endswith(_SHIPPED.note(NOTE_REPEAT_AHEAD, moves="Nf3, Nh3"))
    even = render_playbook(_situation(repeats=repeats), COACH_MODE, chess.WHITE)
    assert "Nf3" not in even


def test_render_specific_tag_replaces_phase_after_margin():
    line = render_playbook(
        _situation(margin=MARGIN_WORSE, structure=STRUCTURE_OPEN, imbalance=IMBALANCE_IQP_THEIRS),
        COACH_MODE, chess.WHITE,
    )
    margin_at = line.index(_frag(AXIS_MARGIN, MARGIN_WORSE))
    iqp_at = line.index(_frag(AXIS_IMBALANCE, IMBALANCE_IQP_THEIRS))
    structure_at = line.index(_frag(AXIS_STRUCTURE, STRUCTURE_OPEN))
    assert margin_at < iqp_at < structure_at
    assert _frag(AXIS_PHASE, PHASE_MIDDLEGAME) not in line


def test_render_specific_tags_in_precedence_order_capped_from_the_tail():
    line = render_playbook(
        _situation(
            imbalance=IMBALANCE_IQP_OURS, bishops=BISHOPS_OPPOSITE_QUEENS,
            pawn=PAWN_PASSED_OUTSIDE_OURS, castling=CASTLING_OPPOSITE,
        ),
        COACH_MODE, chess.WHITE,
    )
    iqp_at = line.index(_frag(AXIS_IMBALANCE, IMBALANCE_IQP_OURS))
    bishops_at = line.index(_frag(AXIS_BISHOPS, BISHOPS_OPPOSITE_QUEENS))
    pawn_at = line.index(_frag(AXIS_PAWN, PAWN_PASSED_OUTSIDE_OURS))
    assert iqp_at < bishops_at < pawn_at
    assert _frag(AXIS_CASTLING, CASTLING_OPPOSITE) not in line


def test_render_phase_leads_in_endgame_with_specific_tags_after():
    line = render_playbook(
        _situation(margin=MARGIN_BETTER, phase=PHASE_ENDGAME, bishops=BISHOPS_OPPOSITE),
        COACH_MODE, chess.WHITE,
    )
    phase_at = line.index(_frag(AXIS_PHASE, PHASE_ENDGAME))
    margin_at = line.index(_frag(AXIS_MARGIN, MARGIN_BETTER))
    bishops_at = line.index(_frag(AXIS_BISHOPS, BISHOPS_OPPOSITE))
    assert phase_at < margin_at < bishops_at


def test_render_structure_combo_specific_tag_replaces_phase():
    line = render_playbook(
        _situation(
            margin=MARGIN_LOSING, structure=STRUCTURE_CLOSED,
            imbalance=IMBALANCE_MINORITY_THEIRS,
        ),
        COACH_MODE, chess.WHITE,
    )
    combo_at = line.index(_SHIPPED.combo(MARGIN_LOSING, AXIS_STRUCTURE, STRUCTURE_CLOSED))
    assert combo_at < line.index(_frag(AXIS_IMBALANCE, IMBALANCE_MINORITY_THEIRS))
    assert _frag(AXIS_PHASE, PHASE_MIDDLEGAME) not in line


def test_render_phase_combo_keeps_specific_then_structure():
    line = render_playbook(
        _situation(
            margin=MARGIN_WORSE, phase=PHASE_OPENING, structure=STRUCTURE_CLOSED,
            castling=CASTLING_OPPOSITE,
        ),
        COACH_MODE, chess.WHITE,
    )
    combo_at = line.index(_SHIPPED.combo(MARGIN_WORSE, AXIS_PHASE, PHASE_OPENING))
    castling_at = line.index(_frag(AXIS_CASTLING, CASTLING_OPPOSITE))
    structure_at = line.index(_frag(AXIS_STRUCTURE, STRUCTURE_CLOSED))
    assert combo_at < castling_at < structure_at


@pytest.mark.parametrize("margin, note", [
    (MARGIN_LOST, _SHIPPED.text(NOTE_LOST)),
    (MARGIN_CRUSHING, _SHIPPED.text(NOTE_CRUSHING)),
])
def test_render_note_wins_its_slot_over_axis_fragments(margin, note):
    # Combo + IQP + bishops would fill all three slots; the note evicts bishops.
    line = render_playbook(
        _situation(
            margin=margin, structure=STRUCTURE_CLOSED,
            imbalance=IMBALANCE_IQP_THEIRS, bishops=BISHOPS_OPPOSITE,
        ),
        COACH_MODE, chess.WHITE,
    )
    assert line.endswith(note)
    assert _frag(AXIS_IMBALANCE, IMBALANCE_IQP_THEIRS) in line
    assert _frag(AXIS_BISHOPS, BISHOPS_OPPOSITE) not in line


def test_render_repetition_note_survives_the_fragment_cap(monkeypatch):
    monkeypatch.setattr(playbook_prompts, "MAX_FRAGMENTS", 1)
    line = render_playbook(
        _situation(margin=MARGIN_LOST, structure=STRUCTURE_CLOSED, repeats=("Nf3",)),
        COACH_MODE, chess.WHITE,
    )
    assert line.endswith(_SHIPPED.note(NOTE_REPEAT_BEHIND, moves="Nf3"))
