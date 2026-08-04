"""HvE difficulty: sample the reply from the search's iteration stream.

The engine is never weakened: one normal full-strength search runs on
the real clock, and below max difficulty the reply is truncated-softmax
sampled from the per-depth best moves that search reported. A shallow
candidate's missed refutation is exactly what a weaker player misses.
See docs/hve-difficulty-spec.md.
"""
from __future__ import annotations

import math
import random

import chess
import chess.engine


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


def collect_iteration(info: chess.engine.InfoDict, out: list) -> None:
    """Pump hook: append (depth, move, white-POV score) for a completed
    iteration. Skips chunks without depth/score/pv, aspiration
    fail-high/low bounds, and MultiPV side lines."""
    if "depth" not in info or "score" not in info:
        return
    pv = info.get("pv")
    if not pv:
        return
    if info.get("lowerbound") or info.get("upperbound"):
        return
    if info.get("multipv", 1) != 1:
        return
    out.append((info["depth"], pv[0], info["score"].pov(chess.WHITE)))


def iteration_candidates(
    iterations: list[tuple[int, chess.Move, chess.engine.Score]],
) -> list[tuple[chess.Move, chess.engine.Score, int]]:
    """Dedup the per-depth best stream to distinct moves, keeping each
    move's deepest (move, score, depth). Every update reinserts, so the
    list is ordered by each move's LAST appearance in the stream --
    effective_drops relies on that to break same-depth anchor ties in
    favor of the engine's latest opinion."""
    by_move: dict[str, tuple[chess.Move, chess.engine.Score, int]] = {}
    for depth, mv, score in iterations:
        key = mv.uci()
        cur = by_move.get(key)
        if cur is None or depth >= cur[2]:
            by_move.pop(key, None)
            by_move[key] = (mv, score, depth)
    return list(by_move.values())


def effective_drops(
    candidates: list[tuple[chess.Move, chess.engine.Score, int]],
    mover: chess.Color,
    clamp_cp: float,
    depth_penalty_cp: float,
) -> list[float]:
    """Per-candidate cost behind the final (deepest) best, in cp.

    cp shortfall is floored at 0 -- a shallow score above the final best
    is optimism the deeper search already refuted, never a bonus -- and
    each depth of shallowness adds depth_penalty_cp, so stale candidates
    fade unless the level's cap/temperature is generous.

    The anchor is the deepest candidate; same-depth ties go to the later
    list position, i.e. the stream's latest opinion (candidates arrive
    ordered by last appearance -- see iteration_candidates)."""
    final = max(enumerate(candidates), key=lambda ic: (ic[1][2], ic[0]))[1]
    final_cp = mover_cp(final[1], mover, clamp_cp)
    drops = []
    for _mv, score, depth in candidates:
        shortfall = max(0.0, final_cp - mover_cp(score, mover, clamp_cp))
        drops.append(shortfall + depth_penalty_cp * (final[2] - depth))
    return drops


def pick_iteration_move(
    iterations: list[tuple[int, chess.Move, chess.engine.Score]],
    mover: chess.Color,
    *,
    temp_cp: float,
    drop_cap_cp: float,
    clamp_cp: float,
    depth_penalty_cp: float,
    rng: random.Random | None = None,
) -> tuple[chess.Move, chess.engine.Score, int] | None:
    """Sample one (move, white-POV score, depth) from the iteration
    stream, or None when the stream had no usable iterations. The final
    best has effective drop 0, so it is always eligible and most
    likely."""
    candidates = iteration_candidates(iterations)
    if not candidates:
        return None
    drops = effective_drops(candidates, mover, clamp_cp, depth_penalty_cp)
    idx = sample_index([-d for d in drops], temp_cp, drop_cap_cp, rng)
    return candidates[idx]
