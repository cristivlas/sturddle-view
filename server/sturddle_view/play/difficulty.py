"""HvE difficulty: full-strength per-move scoring + softmax move sampling.

The engine is never weakened; below max difficulty every legal move is
scored individually and the reply is sampled with a temperature derived
from the difficulty level. See docs/hve-difficulty-spec.md.
"""
from __future__ import annotations

import math
import random

import chess
import chess.engine

# searchmoves compliance probe. Detecting "ignores searchmoves" needs a
# position where the unrestricted choice is predictable and different
# from the probe move. Mate-in-1 gives that: every engine, at any depth,
# plays Ra8# unrestricted. So we send `go searchmoves a1a2`: reply a1a2 =
# restriction honored; anything else = ignored, push-and-eval fallback.
# Kg8 + f7/g7/h7 pawns make it a legal back-rank mate; Kg1 + f2/g2/h2
# mirror it.
PROBE_FEN = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"
PROBE_MOVE_UCI = "a1a2"

# The cosmetic think delay never eats more than this share of the
# engine's remaining clock, so the delay itself cannot flag the engine.
THINK_DELAY_MAX_CLOCK_FRACTION = 0.5


def eval_entry(white_score: chess.engine.Score) -> dict:
    """Eval-history entry ({"cp"|"mate"}, white POV) matching the format
    pump_engine_info captures for full-strength moves."""
    if white_score.is_mate():
        return {"mate": white_score.mate()}
    return {"cp": white_score.score()}


def mover_cp(white_score: chess.engine.Score, mover: chess.Color, clamp: float) -> float:
    """Mover-POV centipawns with mates folded to ~+/-clamp and cp clipped
    to the same range, so a huge eval cannot dominate a found mate."""
    sc = white_score if mover == chess.WHITE else -white_score
    raw = sc.score(mate_score=int(clamp))
    return float(max(-clamp, min(clamp, raw)))


def level_scale_cp(level: int, level_max: int, step_cp: float) -> float:
    """Common shape for the per-level knobs (softmax temperature, drop
    cap): step * levels-below-max. Zero at level_max, growing linearly
    as the level drops."""
    return step_cp * (level_max - level)


def move_weights(
    scores_cp: list[float], temp_cp: float, drop_cap_cp: float,
) -> list[float]:
    """Normalized sampling probabilities over mover-POV scores.

    Truncated softmax: moves more than drop_cap_cp behind the best get
    exactly 0 (the hard blunder-magnitude guarantee); the rest weigh
    exp(drop/temp). temp<=0 degenerates to argmax (uniform over ties).
    Weights are shifted by the best score so exp() cannot overflow."""
    best = max(scores_cp)
    if temp_cp <= 0:
        w = [1.0 if s == best else 0.0 for s in scores_cp]
    else:
        w = [
            math.exp((s - best) / temp_cp) if best - s <= drop_cap_cp else 0.0
            for s in scores_cp
        ]
    total = sum(w)
    return [x / total for x in w]


def sample_index(
    scores_cp: list[float],
    temp_cp: float,
    drop_cap_cp: float,
    rng: random.Random | None = None,
) -> int:
    """Sample an index from move_weights; the best move always has the
    largest probability and over-cap moves are never returned."""
    weights = move_weights(scores_cp, temp_cp, drop_cap_cp)
    return (rng or random).choices(range(len(weights)), weights=weights, k=1)[0]


def think_delay(setting_seconds: float, remaining_seconds: float) -> float:
    """Cosmetic on-clock delay, capped so it can never flag the engine."""
    return min(
        setting_seconds,
        max(0.0, remaining_seconds) * THINK_DELAY_MAX_CLOCK_FRACTION,
    )
