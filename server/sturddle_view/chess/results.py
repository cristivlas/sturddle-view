"""Result string constants and helpers for chess outcomes."""
from __future__ import annotations

WHITE_WIN = "1-0"
BLACK_WIN = "0-1"
DRAW = "1/2-1/2"
DRAW_VARIANTS = frozenset({DRAW, "½-½"})
DECISIVE_RESULTS = frozenset({WHITE_WIN, BLACK_WIN, *DRAW_VARIANTS})


def winner_result(winner_white: bool) -> str:
    return WHITE_WIN if winner_white else BLACK_WIN


def loser_result(loser_white: bool) -> str:
    return BLACK_WIN if loser_white else WHITE_WIN
