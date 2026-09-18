"""Pins and forks on a board -- pure functions, no engine.

Shared by the `tactics` tool (what the model may cite) and the prose
check (what the model did cite). Pins are king/queen shields only; a fork
is one piece attacking two or more targets. Skewers are out of scope.
See docs/ai-analysis-spec.md, Tactical grounding.
"""
from __future__ import annotations

from dataclasses import dataclass

import chess

from ..chess.results import SIDE_BLACK, SIDE_WHITE


# A pinned piece shields one of these behind it on the attacker's ray.
PIN_SHIELD_TYPES = (chess.KING, chess.QUEEN)
# Fork targets that count even when defended.
FORK_MAJOR_TARGETS = (chess.KING, chess.QUEEN, chess.ROOK)

# Ray directions as (file step, rank step); rook rays then bishop rays.
_ROOK_STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_BISHOP_STEPS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
_SLIDER_STEPS = {
    chess.ROOK: _ROOK_STEPS,
    chess.BISHOP: _BISHOP_STEPS,
    chess.QUEEN: _ROOK_STEPS + _BISHOP_STEPS,
}


@dataclass(frozen=True, slots=True)
class Pin:
    """`pinned` (of `color`) sits on `attacker`'s ray in front of `shield`
    (that side's king or queen)."""
    color: chess.Color
    attacker: int
    pinned: int
    shield: int


@dataclass(frozen=True, slots=True)
class Fork:
    """`attacker` (of `color`) attacks every square in `targets`."""
    color: chess.Color
    attacker: int
    targets: tuple[int, ...]


def _ray(square: int, step: tuple[int, int]):
    """Squares from `square` outward along `step`, on-board only."""
    file, rank = chess.square_file(square) + step[0], chess.square_rank(square) + step[1]
    while 0 <= file < 8 and 0 <= rank < 8:
        yield chess.square(file, rank)
        file += step[0]
        rank += step[1]


def _pin_on_ray(board: chess.Board, attacker: int, step: tuple[int, int], color: chess.Color) -> Pin | None:
    """The pin `attacker` makes along `step` against `color`, if any: the
    first piece met is `color`'s (not its king -- that is a check), the
    second is `color`'s king or queen."""
    pinned: int | None = None
    for square in _ray(attacker, step):
        piece = board.piece_at(square)
        if piece is None:
            continue
        if piece.color != color:
            return None
        if pinned is None:
            if piece.piece_type == chess.KING:
                return None
            pinned = square
            continue
        if piece.piece_type in PIN_SHIELD_TYPES:
            return Pin(color, attacker, pinned, square)
        return None
    return None


def pins(board: chess.Board, color: chess.Color) -> list[Pin]:
    """Pins against `color`'s pieces, by every enemy slider."""
    out: list[Pin] = []
    for piece_type, steps in _SLIDER_STEPS.items():
        for attacker in board.pieces(piece_type, not color):
            for step in steps:
                pin = _pin_on_ray(board, attacker, step, color)
                if pin is not None:
                    out.append(pin)
    return out


def _is_fork_target(board: chess.Board, square: int, attacker_type: int) -> bool:
    """A major piece counts defended or not; anything else only when
    undefended. A king attacker can only take undefended pieces."""
    target = board.piece_at(square)
    if target is None:
        return False
    defended = board.is_attacked_by(target.color, square)
    if attacker_type == chess.KING:
        return not defended
    return target.piece_type in FORK_MAJOR_TARGETS or not defended


def forks(board: chess.Board, color: chess.Color) -> list[Fork]:
    """Forks by `color`'s pieces: one attacker, two or more targets."""
    out: list[Fork] = []
    enemy = board.occupied_co[not color]
    for attacker in chess.SquareSet(board.occupied_co[color]):
        attacker_type = board.piece_type_at(attacker)
        targets = tuple(
            square
            for square in chess.SquareSet(board.attacks(attacker) & enemy)
            if _is_fork_target(board, square, attacker_type)
        )
        if len(targets) >= 2:
            out.append(Fork(color, attacker, targets))
    return out


def piece_label(board: chess.Board, square: int) -> str:
    """'black knight on c6' -- the shared wording for tool output and
    prose-check facts."""
    piece = board.piece_at(square)
    color = SIDE_WHITE if piece.color == chess.WHITE else SIDE_BLACK
    return f"{color} {chess.PIECE_NAMES[piece.piece_type]} on {chess.square_name(square)}"


def all_pins(board: chess.Board) -> list[Pin]:
    return pins(board, chess.WHITE) + pins(board, chess.BLACK)


def all_forks(board: chess.Board) -> list[Fork]:
    return forks(board, chess.WHITE) + forks(board, chess.BLACK)
