"""Playbook: situation classifier (play/playbook.py) and fragment rendering
(llm/playbook.py). Positions are built to sit on one side of each
threshold; text assertions check fragment identity, never exact bytes.
"""
from __future__ import annotations

import chess
import pytest

import sturddle_view.llm.playbook as playbook_prompts
from sturddle_view.llm.playbook import (
    _COMBO_FRAGMENTS,
    _CRUSHING_NOTE,
    _LOST_NOTE,
    _MARGIN_FRAGMENTS,
    _PHASE_FRAGMENTS,
    _REPEAT_AHEAD_NOTE,
    _REPEAT_BEHIND_NOTE,
    _STRUCTURE_FRAGMENTS,
    render_playbook,
)
from sturddle_view.llm.prompts import COACH_MODE, COMMENTATOR_MODE, PLAYBOOK_LEAD
from sturddle_view.play.playbook import (
    DECISIVE_CP,
    EDGE_CP,
    MARGIN_BETTER,
    MARGIN_CRUSHING,
    MARGIN_EVEN,
    MARGIN_LOSING,
    MARGIN_LOST,
    MARGIN_WINNING,
    MARGIN_WORSE,
    PHASE_ENDGAME,
    PHASE_LATE,
    PHASE_MIDDLEGAME,
    PHASE_OPENING,
    RESIGN_CP,
    SOURCE_EVAL,
    SOURCE_MATERIAL,
    STRUCTURE_CLOSED,
    STRUCTURE_OPEN,
    Situation,
    classify,
    phase_tag,
    repeating_moves,
    structure_tag,
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


def test_structure_never_tagged_in_endgame():
    # Every file is open with bare kings; the bucket suppresses the tag.
    assert structure_tag(chess.Board(_BARE_KINGS_FEN)) == STRUCTURE_OPEN
    got = classify(chess.Board(_BARE_KINGS_FEN), score_white=None, our_color=chess.WHITE)
    assert got.phase == PHASE_ENDGAME
    assert got.structure is None


# --- rendering -------------------------------------------------------------

def test_render_additive_margin_then_phase_then_structure():
    line = render_playbook(
        _situation(margin=MARGIN_WORSE, phase=PHASE_MIDDLEGAME, structure=STRUCTURE_OPEN),
        COACH_MODE, chess.WHITE,
    )
    assert line.startswith(PLAYBOOK_LEAD)
    assert "you are slightly worse;" in line
    margin_at = line.index(_MARGIN_FRAGMENTS[MARGIN_WORSE])
    phase_at = line.index(_PHASE_FRAGMENTS[PHASE_MIDDLEGAME])
    structure_at = line.index(_STRUCTURE_FRAGMENTS[STRUCTURE_OPEN])
    assert margin_at < phase_at < structure_at


def test_render_phase_leads_in_endgame():
    line = render_playbook(
        _situation(margin=MARGIN_BETTER, phase=PHASE_ENDGAME), COACH_MODE, chess.WHITE,
    )
    assert line.index(_PHASE_FRAGMENTS[PHASE_ENDGAME]) < line.index(_MARGIN_FRAGMENTS[MARGIN_BETTER])


def test_render_even_margin_has_no_margin_fragment():
    line = render_playbook(_situation(), COACH_MODE, chess.WHITE)
    assert "you are level;" in line
    assert _PHASE_FRAGMENTS[PHASE_MIDDLEGAME] in line
    assert not any(f in line for f in _MARGIN_FRAGMENTS.values())


def test_render_combo_replaces_margin_and_phase_keeps_structure():
    line = render_playbook(
        _situation(margin=MARGIN_WINNING, phase=PHASE_ENDGAME, structure=STRUCTURE_OPEN),
        COACH_MODE, chess.WHITE,
    )
    assert _COMBO_FRAGMENTS[(MARGIN_WINNING, PHASE_ENDGAME)] in line
    assert _MARGIN_FRAGMENTS[MARGIN_WINNING] not in line
    assert _PHASE_FRAGMENTS[PHASE_ENDGAME] not in line
    assert _STRUCTURE_FRAGMENTS[STRUCTURE_OPEN] in line


def test_render_structure_combo_keeps_phase():
    line = render_playbook(
        _situation(margin=MARGIN_LOSING, phase=PHASE_MIDDLEGAME, structure=STRUCTURE_CLOSED),
        COACH_MODE, chess.WHITE,
    )
    assert _COMBO_FRAGMENTS[(MARGIN_LOSING, STRUCTURE_CLOSED)] in line
    assert _STRUCTURE_FRAGMENTS[STRUCTURE_CLOSED] not in line
    assert _PHASE_FRAGMENTS[PHASE_MIDDLEGAME] in line


def test_render_extreme_margins_use_base_combo_plus_note():
    lost = render_playbook(
        _situation(margin=MARGIN_LOST, structure=STRUCTURE_OPEN), COACH_MODE, chess.WHITE,
    )
    assert _COMBO_FRAGMENTS[(MARGIN_LOSING, STRUCTURE_OPEN)] in lost
    assert _LOST_NOTE in lost
    crushing = render_playbook(
        _situation(margin=MARGIN_CRUSHING, phase=PHASE_ENDGAME), COMMENTATOR_MODE, chess.BLACK,
    )
    assert _COMBO_FRAGMENTS[(MARGIN_WINNING, PHASE_ENDGAME)] in crushing
    assert _CRUSHING_NOTE in crushing


def test_lost_note_is_coach_only_and_not_in_opening():
    lost = _situation(margin=MARGIN_LOST)
    assert _LOST_NOTE not in render_playbook(lost, COMMENTATOR_MODE, chess.WHITE)
    assert _LOST_NOTE not in render_playbook(
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
    assert _MARGIN_FRAGMENTS[MARGIN_WORSE] in line
    assert _PHASE_FRAGMENTS[PHASE_MIDDLEGAME] not in line
    assert _STRUCTURE_FRAGMENTS[STRUCTURE_OPEN] not in line


def test_render_is_one_line():
    line = render_playbook(
        _situation(margin=MARGIN_LOST, structure=STRUCTURE_CLOSED, repeats=("Nf3",)),
        COACH_MODE, chess.WHITE,
    )
    assert "\n" not in line


def test_render_repetition_note_by_margin():
    repeats = ("Nf3", "Nh3")
    behind = render_playbook(_situation(margin=MARGIN_LOSING, repeats=repeats), COACH_MODE, chess.WHITE)
    assert behind.endswith(_REPEAT_BEHIND_NOTE.format(moves="Nf3, Nh3"))
    ahead = render_playbook(_situation(margin=MARGIN_BETTER, repeats=repeats), COACH_MODE, chess.WHITE)
    assert ahead.endswith(_REPEAT_AHEAD_NOTE.format(moves="Nf3, Nh3"))
    even = render_playbook(_situation(repeats=repeats), COACH_MODE, chess.WHITE)
    assert "Nf3" not in even


def test_render_repetition_note_survives_the_fragment_cap(monkeypatch):
    monkeypatch.setattr(playbook_prompts, "MAX_FRAGMENTS", 1)
    line = render_playbook(
        _situation(margin=MARGIN_LOST, structure=STRUCTURE_CLOSED, repeats=("Nf3",)),
        COACH_MODE, chess.WHITE,
    )
    assert line.endswith(_REPEAT_BEHIND_NOTE.format(moves="Nf3"))
