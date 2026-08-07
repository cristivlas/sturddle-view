"""HvE difficulty: blind the engine's candidate vision, not its brain.

Below max difficulty a shallow off-clock sweep ranks all legal moves;
auto-ranged admission plus a per-move Bernoulli "blinding" roll builds
the visible pool; the engine then runs one normal on-clock search
restricted to the pool and plays its best visible move at full depth.
See docs/hve-difficulty-spec.md.
"""
from __future__ import annotations

import math
import random

import chess
import chess.engine

# searchmoves compliance probe. Detecting "ignores searchmoves" needs a
# position where the unrestricted choice is predictable and different
# from the probe move. Mate-in-1 gives that: every engine, at any depth,
# plays Ra8# unrestricted. So we send `go searchmoves a1a2`: reply a1a2
# = restriction honored; anything else = ignored. Kg8 + f7/g7/h7 pawns
# make it a legal back-rank mate; Kg1 + f2/g2/h2 mirror it.
PROBE_FEN = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"
PROBE_MOVE_UCI = "a1a2"

# Probe search bounds: depth 1 suffices to separate the mate from the
# quiet move; the movetime is only a safety net for engines that ignore
# depth limits.
PROBE_DEPTH = 1
PROBE_MOVETIME_SECONDS = 1.0

# EVT_SYSTEM payload code published (once per engine process) when the
# probe fails and difficulty degrades to full strength. The play
# perspective maps it to a toast.
DIFFICULTY_UNAVAILABLE_ERROR = "difficulty_unavailable"


def mover_cp(white_score: chess.engine.Score, mover: chess.Color, clamp: float) -> float:
    """Mover-POV centipawns with mates folded to ~+/-clamp and cp clipped
    to the same range, so a mate-against outlier cannot stretch the
    auto-ranged score spread."""
    sc = white_score if mover == chess.WHITE else -white_score
    raw = sc.score(mate_score=int(clamp))
    return float(max(-clamp, min(clamp, raw)))


def normalized_gaps(scores_cp: list[float]) -> list[float]:
    """Per-move gap behind the best, normalized by the position's own
    score spread: 0 for the best move, 1 for the worst. A spread of zero
    (all moves equal) yields all-zero gaps."""
    best, worst = max(scores_cp), min(scores_cp)
    spread = best - worst
    if spread <= 0:
        return [0.0] * len(scores_cp)
    return [(best - s) / spread for s in scores_cp]


def win_prob(cp: float, scale_cp: float) -> float:
    """Logistic cp -> win probability; scale_cp sets the sigmoid slope."""
    return 1.0 / (1.0 + math.exp(-cp / scale_cp))


def removal_odds(gap: float, width: float, qmax: float) -> float:
    """Blinding probability for one admitted move: linear decay from
    qmax at the best move to 0 at the admission edge."""
    return qmax * (1.0 - gap / width)


def candidate_pool(
    scores_cp: list[float],
    level: int,
    level_max: int,
    removal_step: float,
    winprob_scale_cp: float,
    winprob_drop_cap: float,
    rng: random.Random | None = None,
) -> list[int]:
    """Indices of the moves the engine is allowed to see.

    Admission is auto-ranged -- normalized gap <= (level_max - level) /
    level_max -- and win-prob capped at every level: the move's win-prob
    drop vs the best must stay under winprob_drop_cap, so no level hangs
    a piece from a healthy position while a losing side keeps a wide
    pool. Each admitted move is then removed with removal_odds(); lower
    levels remove better moves more aggressively. Never empty: if every
    admitted move is blinded, the best one is retained."""
    # level_max never blinds (callers gate on level < max); width 0
    # would divide removal_odds by zero.
    assert level < level_max, "blinding is undefined at max level"
    r = rng or random
    gaps = normalized_gaps(scores_cp)
    width = (level_max - level) / level_max
    qmax = removal_step * (level_max - level)
    best_wp = win_prob(max(scores_cp), winprob_scale_cp)
    admitted = [
        i for i, g in enumerate(gaps)
        if g <= width
        and best_wp - win_prob(scores_cp[i], winprob_scale_cp) <= winprob_drop_cap
    ]
    survivors = [
        i for i in admitted
        if r.random() >= removal_odds(gaps[i], width, qmax)
    ]
    if not survivors:
        survivors = [min(admitted, key=lambda i: gaps[i])]
    return survivors
