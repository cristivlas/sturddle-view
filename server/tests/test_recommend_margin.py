"""Margin logic for recommend_move's dominance check.

`_better_for_stm` decides whether the engine's preferred move is
'strictly better' than the model's candidate. Plain '>' was too tight:
a rival that scores 1cp higher would reject the candidate. We add a
configurable margin so the rival must beat the candidate by at least
that many centipawns from the side-to-move's POV.

Mate scores always trump cp (no margin applies); None scores fall back
to the original conservative behavior.
"""
from __future__ import annotations

import chess
import chess.engine

from sturddle_view.play.tools_engine import _better_for_stm


def _cp(side: chess.Color, value: int) -> chess.engine.PovScore:
    """PovScore from `side`'s POV. Mirrors how engine info arrives."""
    return chess.engine.PovScore(chess.engine.Cp(value), side)


def _mate(side: chess.Color, in_n: int) -> chess.engine.PovScore:
    return chess.engine.PovScore(chess.engine.Mate(in_n), side)


# ---------- default margin (50 cp) -----------------------------------


def test_rival_within_margin_not_better():
    # Candidate +20, rival +60: rival beats by 40, under 50cp margin.
    cand = _cp(chess.WHITE, 20)
    rival = _cp(chess.WHITE, 60)
    assert _better_for_stm(cand, rival, chess.WHITE) is False


def test_rival_beats_by_exactly_margin_not_better():
    # Strict ">", so equal-to-margin doesn't reject.
    cand = _cp(chess.WHITE, 0)
    rival = _cp(chess.WHITE, 50)
    assert _better_for_stm(cand, rival, chess.WHITE) is False


def test_rival_beats_by_more_than_margin_is_better():
    cand = _cp(chess.WHITE, 0)
    rival = _cp(chess.WHITE, 80)
    assert _better_for_stm(cand, rival, chess.WHITE) is True


def test_rival_worse_than_candidate_not_better():
    cand = _cp(chess.WHITE, 100)
    rival = _cp(chess.WHITE, 50)
    assert _better_for_stm(cand, rival, chess.WHITE) is False


# ---------- POV correctness ------------------------------------------


def test_black_to_move_rival_better_by_margin():
    # PovScores stored white-relative; from black's POV we compare
    # negated values. Candidate -30 (white) = +30 black; rival -120
    # (white) = +120 black. Black's rival beats by 90 cp.
    cand = _cp(chess.WHITE, -30)
    rival = _cp(chess.WHITE, -120)
    assert _better_for_stm(cand, rival, chess.BLACK) is True


def test_black_to_move_within_margin():
    cand = _cp(chess.WHITE, -30)
    rival = _cp(chess.WHITE, -70)  # +40 cp better for black
    assert _better_for_stm(cand, rival, chess.BLACK) is False


# ---------- mate trumps cp (margin doesn't apply) ---------------------


def test_rival_mate_always_rejects_cp_candidate():
    # Even with a generous margin, a forced mate trumps a positive cp.
    cand = _cp(chess.WHITE, 500)
    rival = _mate(chess.WHITE, 3)
    assert _better_for_stm(cand, rival, chess.WHITE) is True


def test_candidate_mate_never_rejected_by_cp_rival():
    cand = _mate(chess.WHITE, 5)
    rival = _cp(chess.WHITE, 999)
    assert _better_for_stm(cand, rival, chess.WHITE) is False


# ---------- None fallbacks (unchanged from pre-margin behavior) -------


def test_rival_none_not_better():
    cand = _cp(chess.WHITE, 0)
    assert _better_for_stm(cand, None, chess.WHITE) is False


def test_candidate_none_rival_present_is_better():
    rival = _cp(chess.WHITE, 50)
    assert _better_for_stm(None, rival, chess.WHITE) is True


def test_both_none_not_better():
    assert _better_for_stm(None, None, chess.WHITE) is False


# ---------- margin override (env var) --------------------------------


def test_zero_margin_restores_strict_behavior(monkeypatch):
    # SV_AI_RECOMMEND_MARGIN=0 -> rival > candidate by even 1cp rejects.
    monkeypatch.setenv("SV_AI_RECOMMEND_MARGIN", "0")
    # Force re-read of the constant.
    import importlib
    import sturddle_view.play.tools_engine as te
    importlib.reload(te)
    cand = _cp(chess.WHITE, 0)
    rival = _cp(chess.WHITE, 1)
    assert te._better_for_stm(cand, rival, chess.WHITE) is True
    # Clean up: reload again with the env unset so other tests don't
    # see the strict behavior leak through module state.
    monkeypatch.delenv("SV_AI_RECOMMEND_MARGIN", raising=False)
    importlib.reload(te)
