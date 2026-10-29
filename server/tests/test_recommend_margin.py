"""Margin logic for recommend_move's dominance check.

`_better_for_stm` decides whether the engine's preferred move is
'strictly better' than the model's candidate by more than a margin
(RECOMMEND_MARGIN_CP, one margin regardless of standing).

Mate scores always trump cp (no margin applies); None scores fall back
to the original conservative behavior.
"""
from __future__ import annotations

import importlib

import chess
import chess.engine

import sturddle_view.play.tools_engine as te
from sturddle_view.play.tools_engine import _better_for_stm


_MARGIN = 50


def _cp(side: chess.Color, value: int) -> chess.engine.PovScore:
    """PovScore from `side`'s POV. Mirrors how engine info arrives."""
    return chess.engine.PovScore(chess.engine.Cp(value), side)


def _mate(side: chess.Color, in_n: int) -> chess.engine.PovScore:
    return chess.engine.PovScore(chess.engine.Mate(in_n), side)


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


# ---------- margin knob ----------------------------------------------


def test_margin_env_override(monkeypatch):
    monkeypatch.setenv("SV_AI_RECOMMEND_MARGIN", "10")
    importlib.reload(te)
    try:
        assert te.RECOMMEND_MARGIN_CP == 10
    finally:
        # Reload with the env unset so module state can't leak.
        monkeypatch.delenv("SV_AI_RECOMMEND_MARGIN", raising=False)
        importlib.reload(te)
