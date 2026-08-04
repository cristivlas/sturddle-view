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
    _DEFAULT_HVE_DEPTH_PENALTY_CP,
    _DEFAULT_HVE_DROP_CAP_STEP_CP,
    _DEFAULT_HVE_TEMPERATURE_STEP_CP,
)
from sturddle_view.play.difficulty import (
    collect_iteration,
    effective_drops,
    eval_entry,
    iteration_candidates,
    level_scale_cp,
    move_weights,
    mover_cp,
    pick_iteration_move,
    sample_index,
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


# ----- collect_iteration -----

def _mv(uci):
    return chess.Move.from_uci(uci)


def _info(depth=None, uci=None, cp=None, mate=None, pov=chess.WHITE, **extra):
    info = dict(extra)
    if depth is not None:
        info["depth"] = depth
    if uci is not None:
        info["pv"] = [_mv(uci)]
    if cp is not None or mate is not None:
        score = chess.engine.Mate(mate) if mate is not None else chess.engine.Cp(cp)
        info["score"] = chess.engine.PovScore(score, pov)
    return info


def test_collect_iteration_appends_white_pov():
    out = []
    collect_iteration(_info(depth=5, uci="e2e4", cp=30), out)
    collect_iteration(_info(depth=6, uci="e7e5", cp=-40, pov=chess.BLACK), out)
    assert out == [
        (5, _mv("e2e4"), chess.engine.Cp(30)),
        (6, _mv("e7e5"), chess.engine.Cp(40)),  # black relative -40 = white +40
    ]


@pytest.mark.parametrize("info", [
    _info(uci="e2e4", cp=30),                            # no depth
    _info(depth=5, uci="e2e4"),                          # no score
    _info(depth=5, cp=30),                               # no pv
    _info(depth=5, uci="e2e4", cp=30, lowerbound=True),  # aspiration fail-high
    _info(depth=5, uci="e2e4", cp=30, upperbound=True),  # aspiration fail-low
    _info(depth=5, uci="e2e4", cp=30, multipv=2),        # MultiPV side line
])
def test_collect_iteration_skips_unusable_chunks(info):
    out = []
    collect_iteration(info, out)
    assert out == []


def test_collect_iteration_keeps_explicit_multipv_1():
    out = []
    collect_iteration(_info(depth=5, uci="e2e4", cp=30, multipv=1), out)
    assert len(out) == 1


# ----- iteration_candidates -----

def test_candidates_dedup_keeps_deepest_per_move():
    out = []
    for i in [
        _info(depth=1, uci="e2e4", cp=40),
        _info(depth=2, uci="d2d4", cp=10),
        _info(depth=3, uci="e2e4", cp=25),
    ]:
        collect_iteration(i, out)
    cands = iteration_candidates(out)
    assert sorted((m.uci(), s.score(), d) for m, s, d in cands) == [
        ("d2d4", 10, 2), ("e2e4", 25, 3),
    ]


def test_candidates_ordered_by_last_appearance():
    """An updated move reinserts at the end, so list order tracks each
    move's latest stream appearance (the anchor tie-break relies on it)."""
    out = []
    for i in [
        _info(depth=3, uci="e2e4", cp=30),
        _info(depth=5, uci="d2d4", cp=20),
        _info(depth=8, uci="e2e4", cp=25),
    ]:
        collect_iteration(i, out)
    cands = iteration_candidates(out)
    assert [m.uci() for m, _s, _d in cands] == ["d2d4", "e2e4"]


# ----- effective_drops -----

def test_drops_final_best_is_zero_and_penalty_prices_depth():
    cands = [
        (_mv("e2e4"), chess.engine.Cp(30), 8),   # final
        (_mv("d2d4"), chess.engine.Cp(10), 5),   # 20cp short, 3 shallow
    ]
    drops = effective_drops(cands, chess.WHITE, 1000.0, 15.0)
    assert drops == [0.0, 20.0 + 3 * 15.0]


def test_drops_floor_shallow_optimism_at_zero():
    """A shallow score above the final best is a refuted mirage: its
    cost is depth penalty only, never a bonus."""
    cands = [
        (_mv("e2e4"), chess.engine.Cp(30), 8),
        (_mv("a2a3"), chess.engine.Cp(90), 2),  # mirage: +60 over final
    ]
    drops = effective_drops(cands, chess.WHITE, 1000.0, 15.0)
    assert drops == [0.0, 6 * 15.0]


def test_drops_same_depth_tie_anchors_to_latest_opinion():
    """Engine flip-flops at the final depth: the stream's LAST entry is
    the anchor (drop 0), not the first-seen candidate."""
    out = []
    for i in [
        _info(depth=8, uci="a1b1", cp=10),
        _info(depth=8, uci="a1b2", cp=20),
    ]:
        collect_iteration(i, out)
    drops = effective_drops(iteration_candidates(out), chess.WHITE, 1000.0, 15.0)
    assert drops == [10.0, 0.0]  # a1b1 trails the a1b2 anchor by 10cp


def test_drops_black_mover_pov():
    cands = [
        (_mv("e7e5"), chess.engine.Cp(-30), 8),  # white -30 = black +30: final
        (_mv("d7d5"), chess.engine.Cp(20), 6),   # black -20: 50cp short, 2 shallow
    ]
    drops = effective_drops(cands, chess.BLACK, 1000.0, 15.0)
    assert drops == [0.0, 50.0 + 2 * 15.0]


# ----- pick_iteration_move -----

def _pick(iterations, level, rng=None):
    return pick_iteration_move(
        iterations,
        chess.WHITE,
        temp_cp=_temp(level),
        drop_cap_cp=_cap(level),
        clamp_cp=CLAMP,
        depth_penalty_cp=_DEFAULT_HVE_DEPTH_PENALTY_CP,
        rng=rng,
    )


def test_pick_empty_stream_returns_none():
    assert _pick([], 5) is None


def test_pick_high_level_excludes_stale_candidates():
    """Level 9 (cap 50): a candidate 6 depths stale costs 90 in penalty
    alone -- excluded, final best always played."""
    iters = [
        (2, _mv("a2a3"), chess.engine.Cp(40)),
        (8, _mv("e2e4"), chess.engine.Cp(30)),
    ]
    for _ in range(20):
        assert _pick(iters, 9)[0] == _mv("e2e4")


def test_pick_low_level_samples_shallow_candidates():
    """Level 1 (cap 450, temp 225): the stale candidate is genuinely in
    play -- both moves appear across seeded draws."""
    iters = [
        (2, _mv("a2a3"), chess.engine.Cp(40)),
        (8, _mv("e2e4"), chess.engine.Cp(30)),
    ]
    rng = random.Random(3)
    seen = {_pick(iters, 1, rng)[0].uci() for _ in range(200)}
    assert seen == {"a2a3", "e2e4"}


def test_pick_returns_candidate_score_and_depth():
    iters = [(8, _mv("e2e4"), chess.engine.Cp(30))]
    assert _pick(iters, 5) == (_mv("e2e4"), chess.engine.Cp(30), 8)


# ----- config wiring -----

def test_difficulty_is_persisted_setting():
    assert "hve_difficulty" in PERSISTED_FIELDS
