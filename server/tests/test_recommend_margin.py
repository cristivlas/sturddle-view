"""Margin logic for recommend_move's dominance check.

`_better_for_stm` decides whether the engine's preferred move is
'strictly better' than the model's candidate by more than a margin;
`recommend_margin_cp` picks that margin from the side to move's
Situation: MIN ahead (convert cleanly), MAX behind (practical chances),
the midpoint when even or unclassified.

Mate scores always trump cp (no margin applies); None scores fall back
to the original conservative behavior.
"""
from __future__ import annotations

import importlib

import chess
import chess.engine
import pytest

import sturddle_view.play.tools_engine as te
from sturddle_view.play.playbook import (
    MARGIN_BETTER,
    MARGIN_CRUSHING,
    MARGIN_EVEN,
    MARGIN_LOSING,
    MARGIN_LOST,
    MARGIN_WINNING,
    MARGIN_WORSE,
    PHASE_MIDDLEGAME,
    SOURCE_EVAL,
    Situation,
)
from sturddle_view.play.tools_engine import (
    RECOMMEND_MARGIN_MAX_CP,
    RECOMMEND_MARGIN_MIN_CP,
    _better_for_stm,
    recommend_margin_cp,
)


_MARGIN = 50


def _cp(side: chess.Color, value: int) -> chess.engine.PovScore:
    """PovScore from `side`'s POV. Mirrors how engine info arrives."""
    return chess.engine.PovScore(chess.engine.Cp(value), side)


def _mate(side: chess.Color, in_n: int) -> chess.engine.PovScore:
    return chess.engine.PovScore(chess.engine.Mate(in_n), side)


def _situation(margin: str) -> Situation:
    return Situation(margin, SOURCE_EVAL, PHASE_MIDDLEGAME, None)


# ---------- predicate at a fixed margin ------------------------------


def test_rival_within_margin_not_better():
    assert _better_for_stm(_cp(chess.WHITE, 20), _cp(chess.WHITE, 60), chess.WHITE, _MARGIN) is False


def test_rival_beats_by_exactly_margin_not_better():
    # Strict ">", so equal-to-margin doesn't reject.
    assert _better_for_stm(_cp(chess.WHITE, 0), _cp(chess.WHITE, 50), chess.WHITE, _MARGIN) is False


def test_rival_beats_by_more_than_margin_is_better():
    assert _better_for_stm(_cp(chess.WHITE, 0), _cp(chess.WHITE, 80), chess.WHITE, _MARGIN) is True


def test_rival_worse_than_candidate_not_better():
    assert _better_for_stm(_cp(chess.WHITE, 100), _cp(chess.WHITE, 50), chess.WHITE, _MARGIN) is False


def test_zero_margin_is_strict():
    assert _better_for_stm(_cp(chess.WHITE, 0), _cp(chess.WHITE, 1), chess.WHITE, 0) is True


# ---------- POV correctness ------------------------------------------


def test_black_to_move_rival_better_by_margin():
    # PovScores stored white-relative; from black's POV we compare
    # negated values. Candidate -30 (white) = +30 black; rival -120
    # (white) = +120 black. Black's rival beats by 90 cp.
    assert _better_for_stm(_cp(chess.WHITE, -30), _cp(chess.WHITE, -120), chess.BLACK, _MARGIN) is True


def test_black_to_move_within_margin():
    assert _better_for_stm(_cp(chess.WHITE, -30), _cp(chess.WHITE, -70), chess.BLACK, _MARGIN) is False


# ---------- mate trumps cp (margin doesn't apply) ---------------------


def test_rival_mate_always_rejects_cp_candidate():
    assert _better_for_stm(_cp(chess.WHITE, 500), _mate(chess.WHITE, 3), chess.WHITE, _MARGIN) is True


def test_candidate_mate_never_rejected_by_cp_rival():
    assert _better_for_stm(_mate(chess.WHITE, 5), _cp(chess.WHITE, 999), chess.WHITE, _MARGIN) is False


# ---------- None fallbacks -------------------------------------------


def test_rival_none_not_better():
    assert _better_for_stm(_cp(chess.WHITE, 0), None, chess.WHITE, _MARGIN) is False


def test_candidate_none_rival_present_is_better():
    assert _better_for_stm(None, _cp(chess.WHITE, 50), chess.WHITE, _MARGIN) is True


def test_both_none_not_better():
    assert _better_for_stm(None, None, chess.WHITE, _MARGIN) is False


# ---------- plan-aware margin ----------------------------------------


@pytest.mark.parametrize("margin", [MARGIN_BETTER, MARGIN_WINNING, MARGIN_CRUSHING])
def test_ahead_uses_min_margin(margin):
    assert recommend_margin_cp(_situation(margin)) == RECOMMEND_MARGIN_MIN_CP


@pytest.mark.parametrize("margin", [MARGIN_WORSE, MARGIN_LOSING, MARGIN_LOST])
def test_behind_uses_max_margin(margin):
    assert recommend_margin_cp(_situation(margin)) == RECOMMEND_MARGIN_MAX_CP


def test_even_and_unclassified_use_midpoint():
    mid = (RECOMMEND_MARGIN_MIN_CP + RECOMMEND_MARGIN_MAX_CP) // 2
    assert recommend_margin_cp(_situation(MARGIN_EVEN)) == mid
    assert recommend_margin_cp(None) == mid


def test_min_max_env_override(monkeypatch):
    monkeypatch.setenv("SV_AI_RECOMMEND_MARGIN_MIN", "10")
    monkeypatch.setenv("SV_AI_RECOMMEND_MARGIN_MAX", "30")
    importlib.reload(te)
    try:
        assert te.recommend_margin_cp(_situation(MARGIN_WINNING)) == 10
        assert te.recommend_margin_cp(_situation(MARGIN_LOSING)) == 30
        assert te.recommend_margin_cp(None) == 20
    finally:
        # Reload with the env unset so module state can't leak.
        monkeypatch.delenv("SV_AI_RECOMMEND_MARGIN_MIN", raising=False)
        monkeypatch.delenv("SV_AI_RECOMMEND_MARGIN_MAX", raising=False)
        importlib.reload(te)
