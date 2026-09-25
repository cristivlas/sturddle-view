"""Engine score dicts: exactly one of centipawns or mate-in-N, plus
optional extras such as the search depth."""
from __future__ import annotations

SCORE_CP = "cp"
SCORE_MATE = "mate"
SCORE_DEPTH = "depth"
CP_PER_PAWN = 100


def flip_score(score: dict) -> dict:
    """The same score from the other side's point of view."""
    for key in (SCORE_MATE, SCORE_CP):
        if key in score:
            return {**score, key: -score[key]}
    return score
