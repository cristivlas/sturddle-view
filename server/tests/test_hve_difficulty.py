"""Pure-logic tests for the HvE difficulty blinding helpers.

The contract: pool admission auto-ranges off the position's own score
spread intersected with a win-prob drop cap (tight near equality,
self-loosening when behind), the best move is always admitted, blinding
odds decay linearly across the admitted band, and the pool is never
empty. Randomness is exercised with seeded or scripted rngs only.
"""
from __future__ import annotations

import random

import chess
import chess.engine
import pytest

from sturddle_view.config import (
    HVE_DIFFICULTY_MAX,
    HVE_DIFFICULTY_MIN,
    PERSISTED_FIELDS,
    _DEFAULT_HVE_REMOVAL_STEP,
    _DEFAULT_HVE_WINPROB_DROP_CAP,
    _DEFAULT_HVE_WINPROB_SCALE_CP,
)
from sturddle_view.play.difficulty import (
    candidate_pool,
    mover_cp,
    normalized_gaps,
    removal_odds,
    win_prob,
)

CLAMP = 1000.0

# Default win-prob gate args, in candidate_pool positional order.
WP_ARGS = (_DEFAULT_HVE_WINPROB_SCALE_CP, _DEFAULT_HVE_WINPROB_DROP_CAP)

# A realistic mover-POV spread: near-best cluster, a positional slip,
# a hung pawn, a hung piece, a hung queen. At the default scale the
# win-prob cap admits only [0..3] (the -120 pawn hang drops 0.19).
REALISTIC = [20.0, 10.0, 0.0, -40.0, -120.0, -350.0, -900.0]

# A flat spread entirely inside the win-prob cap: only the relative
# width and the blinding rolls act on it.
QUIET = [0.0, -20.0, -40.0, -60.0, -80.0, -100.0]

ALL_LEVELS = range(HVE_DIFFICULTY_MIN, HVE_DIFFICULTY_MAX)


class ScriptedRng:
    """random() returns scripted values in order (cycling)."""

    def __init__(self, values):
        self._values = list(values)
        self._i = 0

    def random(self):
        v = self._values[self._i % len(self._values)]
        self._i += 1
        return v


KEEP_ALL = ScriptedRng([1.0])   # random() = 1.0 >= any q: nothing removed
REMOVE_ALL = ScriptedRng([0.0])  # random() = 0.0 < any q > 0: all removed


# ----- normalized_gaps -----

def test_gaps_zero_for_best_one_for_worst():
    gaps = normalized_gaps(REALISTIC)
    assert gaps[0] == 0.0
    assert gaps[-1] == 1.0
    assert gaps == sorted(gaps)


def test_gaps_all_equal_scores_yield_zeros():
    assert normalized_gaps([50.0, 50.0, 50.0]) == [0.0, 0.0, 0.0]


# ----- win_prob -----

def test_win_prob_midpoint_and_symmetry():
    scale = _DEFAULT_HVE_WINPROB_SCALE_CP
    assert win_prob(0.0, scale) == 0.5
    assert win_prob(200.0, scale) + win_prob(-200.0, scale) == pytest.approx(1.0)
    assert win_prob(300.0, scale) > win_prob(100.0, scale) > win_prob(-100.0, scale)


# ----- removal_odds -----

def test_removal_odds_peak_at_best_zero_at_edge():
    assert removal_odds(0.0, 0.5, 0.8) == 0.8
    assert removal_odds(0.5, 0.5, 0.8) == 0.0
    assert removal_odds(0.25, 0.5, 0.8) == pytest.approx(0.4)


# ----- candidate_pool: admission -----

@pytest.mark.parametrize("level", ALL_LEVELS)
def test_admission_gates_recomputed_per_level(level):
    """Admission is normalized gap <= (max-level)/max intersected with
    the win-prob drop cap; verified against a direct recomputation,
    blinding disabled."""
    pool = candidate_pool(REALISTIC, level, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=KEEP_ALL)
    gaps = normalized_gaps(REALISTIC)
    width = (HVE_DIFFICULTY_MAX - level) / HVE_DIFFICULTY_MAX
    best_wp = win_prob(max(REALISTIC), _DEFAULT_HVE_WINPROB_SCALE_CP)
    assert pool == [
        i for i, g in enumerate(gaps)
        if g <= width
        and best_wp - win_prob(REALISTIC[i], _DEFAULT_HVE_WINPROB_SCALE_CP)
        <= _DEFAULT_HVE_WINPROB_DROP_CAP
    ]


def test_best_move_always_admitted():
    for level in ALL_LEVELS:
        pool = candidate_pool(REALISTIC, level, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=KEEP_ALL)
        assert 0 in pool


def test_level_9_admits_only_top_slice():
    """Level 9: width 0.1 of a 920cp spread admits gaps up to 92cp --
    the 20/10/0/-40 cluster, nothing deeper."""
    pool = candidate_pool(REALISTIC, 9, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=KEEP_ALL)
    assert pool == [0, 1, 2, 3]


def test_level_1_capped_by_win_prob():
    """Level 1: width 0.9 would reach the -350 piece hang, but the
    win-prob cap already stops at the -120 pawn hang (drop 0.19)."""
    pool = candidate_pool(REALISTIC, 1, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=KEEP_ALL)
    assert pool == [0, 1, 2, 3]


def test_winprob_cap_blocks_hang_near_equality():
    """Near equality a 250cp drop is a 0.30 win-prob drop: excluded at
    every level even though the relative width admits it."""
    pool = candidate_pool([0.0, -250.0, -900.0], 1, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=KEEP_ALL)
    assert pool == [0]


def test_winprob_cap_loosens_when_behind():
    """The same 250cp drop from an already-lost -300 is only a 0.11
    win-prob drop: admitted, so a losing side keeps a wide pool."""
    pool = candidate_pool([-300.0, -550.0, -900.0], 1, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=KEEP_ALL)
    assert pool == [0, 1]


# ----- candidate_pool: blinding -----

def test_blinding_never_empties_pool():
    pool = candidate_pool(REALISTIC, 1, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=REMOVE_ALL)
    assert pool == [0]  # all blinded -> best survivor retained


def test_blinding_removes_only_probabilistically_targeted():
    """Scripted rng: first roll (best move) below its q -> removed;
    later rolls high -> kept. The pool loses exactly the best move."""
    rng = ScriptedRng([0.0, 1.0, 1.0, 1.0])
    pool = candidate_pool(REALISTIC, 1, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=rng)
    assert pool == [1, 2, 3]


def test_blinding_odds_fall_with_gap():
    """Statistically: over seeded rolls at level 1, a near-best move is
    removed more often than one deep in the band. (Index 0 is excluded
    from the comparison: the never-empty fallback re-admits it, skewing
    its observed rate.)"""
    rng = random.Random(7)
    removed_near = removed_far = 0
    for _ in range(500):
        pool = candidate_pool(QUIET, 1, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=rng)
        removed_near += 1 not in pool
        removed_far += 4 not in pool
    assert removed_near > removed_far


def test_level_9_blinding_is_mild():
    """qmax at level 9 is one removal_step: the best move survives the
    vast majority of seeded rolls."""
    rng = random.Random(11)
    kept = sum(
        0 in candidate_pool(REALISTIC, 9, HVE_DIFFICULTY_MAX, 0.10, *WP_ARGS, rng=rng)
        for _ in range(500)
    )
    assert kept > 400


def test_default_removal_step_sane():
    assert 0.0 < _DEFAULT_HVE_REMOVAL_STEP * (HVE_DIFFICULTY_MAX - 1) < 1.0


# ----- mover_cp -----

def test_mover_cp_white_identity_black_negates():
    sc = chess.engine.Cp(120)
    assert mover_cp(sc, chess.WHITE, CLAMP) == 120.0
    assert mover_cp(sc, chess.BLACK, CLAMP) == -120.0


def test_mover_cp_folds_mates_to_clamp_magnitude():
    win = chess.engine.Mate(3)
    loss = chess.engine.Mate(-2)
    assert mover_cp(win, chess.WHITE, CLAMP) == 997.0
    assert mover_cp(win, chess.BLACK, CLAMP) == -997.0
    assert mover_cp(loss, chess.WHITE, CLAMP) == -998.0
    assert mover_cp(loss, chess.BLACK, CLAMP) == 998.0


def test_mover_cp_clips_huge_cp_to_clamp():
    assert mover_cp(chess.engine.Cp(1500), chess.WHITE, CLAMP) == CLAMP
    assert mover_cp(chess.engine.Cp(-1500), chess.WHITE, CLAMP) == -CLAMP


# ----- config wiring -----

def test_difficulty_is_persisted_setting():
    assert "hve_difficulty" in PERSISTED_FIELDS
