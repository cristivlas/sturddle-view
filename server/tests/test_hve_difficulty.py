"""Pure-logic tests for the HvE difficulty helpers (play/difficulty.py).

The blunder-magnitude contract: softmax temperature shapes error SIZE
(worse moves exponentially less likely), the drop cap makes over-cap
blunders impossible -- not merely rare. Probabilities are asserted
analytically from move_weights; sampling uses a seeded rng. No engine,
no waits.
"""
from __future__ import annotations

import math
import random

import chess
import chess.engine
import pytest

from sturddle_view.config import (
    HVE_DIFFICULTY_MAX,
    HVE_DIFFICULTY_MIN,
    PERSISTED_FIELDS,
    _DEFAULT_HVE_DROP_CAP_STEP_CP,
    _DEFAULT_HVE_TEMPERATURE_STEP_CP,
)
from sturddle_view.play.difficulty import (
    THINK_DELAY_MAX_CLOCK_FRACTION,
    eval_entry,
    level_scale_cp,
    move_weights,
    mover_cp,
    sample_index,
    think_delay,
)

CLAMP = 1000.0

# A realistic mover-POV score spread: near-best cluster, a positional
# slip, a hung pawn, a hung piece, a hung queen/mate-adjacent howler.
REALISTIC = [20.0, 10.0, 0.0, -40.0, -120.0, -350.0, -900.0]

ALL_LEVELS = range(HVE_DIFFICULTY_MIN, HVE_DIFFICULTY_MAX)


def _temp(level: int) -> float:
    return level_scale_cp(level, HVE_DIFFICULTY_MAX, _DEFAULT_HVE_TEMPERATURE_STEP_CP)


def _cap(level: int) -> float:
    return level_scale_cp(level, HVE_DIFFICULTY_MAX, _DEFAULT_HVE_DROP_CAP_STEP_CP)


def _weights(level: int, scores: list[float]) -> list[float]:
    return move_weights(scores, _temp(level), _cap(level))


# ----- level_scale_cp -----

def test_level_scale_zero_at_max():
    assert level_scale_cp(HVE_DIFFICULTY_MAX, HVE_DIFFICULTY_MAX, 25.0) == 0.0


def test_level_scale_monotonic_decreasing_in_level():
    scales = [level_scale_cp(lv, HVE_DIFFICULTY_MAX, 25.0) for lv in ALL_LEVELS]
    assert scales == sorted(scales, reverse=True)
    assert all(s > 0 for s in scales)


# ----- move_weights: distribution shape -----

@pytest.mark.parametrize("level", ALL_LEVELS)
def test_weights_sum_to_one(level):
    assert math.isclose(sum(_weights(level, REALISTIC)), 1.0)


def test_zero_temp_is_argmax():
    w = move_weights(REALISTIC, 0.0, 100.0)
    assert w[0] == 1.0
    assert all(x == 0.0 for x in w[1:])


def test_zero_temp_uniform_over_ties():
    w = move_weights([50.0, 50.0, -10.0], 0.0, 100.0)
    assert w == [0.5, 0.5, 0.0]


@pytest.mark.parametrize("level", ALL_LEVELS)
def test_worse_move_never_likelier(level):
    """Monotonicity: within any level, a worse move never outweighs a
    better one -- magnitude stays proportional."""
    w = _weights(level, REALISTIC)
    assert w == sorted(w, reverse=True)


def test_best_move_probability_rises_with_level():
    probs = [_weights(lv, REALISTIC)[0] for lv in ALL_LEVELS]
    assert probs == sorted(probs)


def test_expected_drop_shrinks_as_level_rises():
    """The user-facing contract: average goofup magnitude is inverse to
    difficulty. Computed analytically, no sampling."""
    best = max(REALISTIC)
    expected = []
    for lv in ALL_LEVELS:
        w = _weights(lv, REALISTIC)
        expected.append(sum(p * (best - s) for p, s in zip(w, REALISTIC)))
    assert expected == sorted(expected, reverse=True)


def test_within_cap_follows_softmax_ratio():
    scores = [0.0, -50.0]
    w = move_weights(scores, 100.0, 200.0)
    assert math.isclose(w[1] / w[0], math.exp(-50.0 / 100.0))


def test_no_overflow_at_clamp_extremes():
    w = move_weights([CLAMP, -CLAMP], 1.0, 2 * CLAMP)
    assert math.isclose(sum(w), 1.0)
    assert w[0] > w[1] >= 0.0


# ----- move_weights: the hard drop-cap guarantee -----

def test_over_cap_weight_exactly_zero():
    w = move_weights([0.0, -201.0], 100.0, 200.0)
    assert w == [1.0, 0.0]


def test_at_cap_boundary_still_sampleable():
    w = move_weights([0.0, -200.0], 100.0, 200.0)
    assert w[1] > 0.0


@pytest.mark.parametrize("level", range(5, HVE_DIFFICULTY_MAX))
def test_hung_piece_impossible_at_level_5_and_above(level):
    """Difficulty >= 5 must never play a move that puts it behind by a
    lot: the -350 and -900 candidates get exactly zero probability."""
    w = _weights(level, REALISTIC)
    cap = _cap(level)
    for weight, score in zip(w, REALISTIC):
        if max(REALISTIC) - score > cap:
            assert weight == 0.0
    assert w[-1] == 0.0  # -900 hang: excluded at every level >= 5
    assert w[-2] == 0.0  # -350 hang: cap at level 5 is 250


def test_cap_relative_to_best_when_all_moves_lose():
    """In a lost position (all moves bad) the cap is relative to the
    least-bad move, so sampling always has at least one candidate."""
    w = move_weights([-800.0, -820.0, -1000.0], 100.0, 50.0)
    assert w[0] > 0.0
    assert w[2] == 0.0
    assert math.isclose(sum(w), 1.0)


# ----- sample_index -----

def test_sample_deterministic_when_cap_leaves_only_best():
    scores = [0.0, -300.0, -500.0]
    for _ in range(20):
        assert sample_index(scores, 50.0, 100.0) == 0


def test_sample_never_returns_over_cap_move():
    rng = random.Random(42)
    cap = _cap(5)
    best = max(REALISTIC)
    for _ in range(2000):
        idx = sample_index(REALISTIC, _temp(5), cap, rng)
        assert best - REALISTIC[idx] <= cap


def test_sample_seeded_rng_is_deterministic():
    a = [sample_index(REALISTIC, _temp(3), _cap(3), random.Random(7)) for _ in range(10)]
    b = [sample_index(REALISTIC, _temp(3), _cap(3), random.Random(7)) for _ in range(10)]
    assert a == b


def test_sample_prefers_better_moves():
    rng = random.Random(11)
    counts = [0, 0]
    for _ in range(2000):
        counts[sample_index([0.0, -60.0], 100.0, 500.0, rng)] += 1
    assert counts[0] > counts[1]


# ----- mover_cp -----

def test_mover_cp_white_identity_black_negates():
    sc = chess.engine.Cp(120)
    assert mover_cp(sc, chess.WHITE, CLAMP) == 120.0
    assert mover_cp(sc, chess.BLACK, CLAMP) == -120.0


def test_mover_cp_folds_mates_to_clamp_magnitude():
    win = chess.engine.Mate(3)   # white mates in 3 (white POV)
    loss = chess.engine.Mate(-2)
    assert mover_cp(win, chess.WHITE, CLAMP) == 997.0
    assert mover_cp(win, chess.BLACK, CLAMP) == -997.0
    assert mover_cp(loss, chess.WHITE, CLAMP) == -998.0
    assert mover_cp(loss, chess.BLACK, CLAMP) == 998.0


def test_mover_cp_clips_huge_cp_to_clamp():
    assert mover_cp(chess.engine.Cp(1500), chess.WHITE, CLAMP) == CLAMP
    assert mover_cp(chess.engine.Cp(-1500), chess.WHITE, CLAMP) == -CLAMP


def test_mover_cp_huge_eval_cannot_dominate_mate():
    """Clipping keeps a clipped +1500 eval within 1cp of a mate-in-1:
    the clamp prevents huge evals from dwarfing found mates."""
    eval_cp = mover_cp(chess.engine.Cp(1500), chess.WHITE, CLAMP)
    mate_cp = mover_cp(chess.engine.Mate(1), chess.WHITE, CLAMP)
    assert abs(eval_cp - mate_cp) <= 1.0


# ----- eval_entry -----

def test_eval_entry_formats():
    assert eval_entry(chess.engine.Cp(34)) == {"cp": 34}
    assert eval_entry(chess.engine.Cp(-7)) == {"cp": -7}
    assert eval_entry(chess.engine.Mate(2)) == {"mate": 2}
    assert eval_entry(chess.engine.Mate(-4)) == {"mate": -4}


# ----- think_delay -----

def test_think_delay_setting_wins_when_clock_is_healthy():
    assert think_delay(1.0, 60.0) == 1.0


def test_think_delay_capped_at_clock_fraction():
    assert think_delay(1.0, 1.0) == 1.0 * THINK_DELAY_MAX_CLOCK_FRACTION


def test_think_delay_zero_and_negative_remaining():
    assert think_delay(1.0, 0.0) == 0.0
    assert think_delay(1.0, -5.0) == 0.0


# ----- config wiring -----

def test_difficulty_is_persisted_setting():
    assert "hve_difficulty" in PERSISTED_FIELDS
