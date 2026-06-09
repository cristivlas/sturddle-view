"""Result string constants and helpers for chess outcomes."""
from __future__ import annotations

WHITE_WIN = "1-0"
BLACK_WIN = "0-1"
DRAW = "1/2-1/2"
DECISIVE_RESULTS = frozenset({WHITE_WIN, BLACK_WIN, DRAW})

# Side-to-move wire strings. Emitted in the board_update/clock_tick `turn`
# field and other STM payloads; the client switches on the same values.
SIDE_WHITE = "white"
SIDE_BLACK = "black"


def winner_result(winner_white: bool) -> str:
    return WHITE_WIN if winner_white else BLACK_WIN


def loser_result(loser_white: bool) -> str:
    return BLACK_WIN if loser_white else WHITE_WIN
