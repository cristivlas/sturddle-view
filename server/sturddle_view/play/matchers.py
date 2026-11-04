"""Playbook matchers: pattern recognizers behind one protocol
(docs/ai-playbook-matchers-spec.md).

A matcher reads a MatchContext (board plus the base tags, margin and
phase) and returns the tags it recognizes. Pure functions of the
context: no engine, no I/O. REGISTRY order is render order for the
specific tags. Thresholds are named constants with SV_ env overrides.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import chess

from ..env_utils import env_int
from .playbook_tags import (
    AXIS_BISHOPS,
    AXIS_CASTLING,
    AXIS_IMBALANCE,
    AXIS_PAWN,
    AXIS_STRUCTURE,
    BISHOPS_OPPOSITE,
    BISHOPS_OPPOSITE_QUEENS,
    CASTLING_OPPOSITE,
    IMBALANCE_IQP_OURS,
    IMBALANCE_IQP_THEIRS,
    IMBALANCE_MINORITY_OURS,
    IMBALANCE_MINORITY_THEIRS,
    PAWN_PASSED_OUTSIDE_OURS,
    PAWN_PASSED_OUTSIDE_THEIRS,
    PHASE_ENDGAME,
    STRUCTURE_CLOSED,
    STRUCTURE_OPEN,
)


# Structure: locked pawn pairs and open / half-open files.
_DEFAULT_CLOSED_MIN_LOCKED = 3
_DEFAULT_CLOSED_MAX_OPEN_FILES = 1
_DEFAULT_OPEN_MIN_FILES = 4
CLOSED_MIN_LOCKED = env_int("SV_AI_PLAYBOOK_CLOSED_MIN_LOCKED", _DEFAULT_CLOSED_MIN_LOCKED)
CLOSED_MAX_OPEN_FILES = env_int(
    "SV_AI_PLAYBOOK_CLOSED_MAX_OPEN_FILES", _DEFAULT_CLOSED_MAX_OPEN_FILES,
)
OPEN_MIN_FILES = env_int("SV_AI_PLAYBOOK_OPEN_MIN_FILES", _DEFAULT_OPEN_MIN_FILES)

# Board geometry for the specific-tag predicates.
_IQP_NEIGHBOR_FILES = chess.BB_FILE_C | chess.BB_FILE_E
_MINORITY_FILES = (chess.BB_FILE_A, chess.BB_FILE_B)
_MAJORITY_FILES = (*_MINORITY_FILES, chess.BB_FILE_C)
_OUTSIDE_FILES = chess.BB_FILE_A | chess.BB_FILE_B | chess.BB_FILE_G | chess.BB_FILE_H
_QUEENSIDE_KING_FILES = chess.BB_FILE_A | chess.BB_FILE_B | chess.BB_FILE_C
_KINGSIDE_KING_FILES = chess.BB_FILE_G | chess.BB_FILE_H
_WHITE_SHELTER_RANKS = chess.BB_RANK_1 | chess.BB_RANK_2
_BLACK_SHELTER_RANKS = chess.BB_RANK_7 | chess.BB_RANK_8


@dataclass(frozen=True, slots=True)
class MatchContext:
    board: chess.Board
    our_color: chess.Color
    margin: str
    margin_source: str
    phase: str


@dataclass(frozen=True, slots=True)
class Match:
    axis: str
    tag: str


class Matcher(Protocol):
    name: str
    # Axis -> every tag the matcher can emit on it.
    tags: Mapping[str, frozenset[str]]

    def match(self, ctx: MatchContext) -> Sequence[Match]: ...


@dataclass(frozen=True, slots=True)
class PredicateMatcher:
    """One axis, at most one tag, from a predicate over the context."""
    name: str
    tags: Mapping[str, frozenset[str]]
    predicate: Callable[[MatchContext], str | None]

    def match(self, ctx: MatchContext) -> Sequence[Match]:
        tag = self.predicate(ctx)
        if tag is None:
            return ()
        (axis,) = self.tags
        return (Match(axis, tag),)


def _locked(board: chess.Board) -> chess.Bitboard:
    """Black pawns directly in front of a white pawn."""
    white = board.pieces_mask(chess.PAWN, chess.WHITE)
    black = board.pieces_mask(chess.PAWN, chess.BLACK)
    return chess.shift_up(white) & black


def _locked_pairs(board: chess.Board) -> int:
    return chess.popcount(_locked(board))


def _file_counts(board: chess.Board) -> tuple[int, int]:
    """(open files, half-open files) by pawn presence."""
    white = board.pieces(chess.PAWN, chess.WHITE)
    black = board.pieces(chess.PAWN, chess.BLACK)
    open_files = half_open = 0
    for file_bb in chess.BB_FILES:
        has_white = bool(white & file_bb)
        has_black = bool(black & file_bb)
        if not has_white and not has_black:
            open_files += 1
        elif has_white != has_black:
            half_open += 1
    return open_files, half_open


def structure_tag(board: chess.Board) -> str | None:
    open_files, half_open = _file_counts(board)
    if _locked_pairs(board) >= CLOSED_MIN_LOCKED and open_files <= CLOSED_MAX_OPEN_FILES:
        return STRUCTURE_CLOSED
    if open_files + half_open >= OPEN_MIN_FILES:
        return STRUCTURE_OPEN
    return None


def _single(bb: chess.Bitboard) -> bool:
    """Exactly one square set."""
    return bool(bb) and chess.lsb(bb) == chess.msb(bb)


def _pov(side: chess.Color | None, our_color: chess.Color, ours: str, theirs: str) -> str | None:
    if side is None:
        return None
    return ours if side == our_color else theirs


def _front_span(square: chess.Square, color: chess.Color) -> chess.Bitboard:
    """Squares ahead of `square` for `color` on its own and the adjacent files."""
    step = chess.shift_up if color == chess.WHITE else chess.shift_down
    bb = chess.BB_SQUARES[square]
    bb |= chess.shift_left(bb) | chess.shift_right(bb)
    span = chess.BB_EMPTY
    while bb := step(bb):
        span |= bb
    return span


def _passed_pawns(board: chess.Board, color: chess.Color) -> chess.SquareSet:
    """Pawns of `color` with no enemy pawn ahead on their own or an adjacent file."""
    enemy = board.pieces_mask(chess.PAWN, not color)
    return chess.SquareSet(
        sq for sq in board.pieces(chess.PAWN, color) if not _front_span(sq, color) & enemy
    )


def _iqp_side(board: chess.Board) -> chess.Color | None:
    """The side with an isolated, non-passed d-pawn facing an empty enemy d-file."""
    for color in chess.COLORS:
        own = board.pieces_mask(chess.PAWN, color)
        d_pawn = own & chess.BB_FILE_D
        if (
            _single(d_pawn)
            and not own & _IQP_NEIGHBOR_FILES
            and not board.pieces_mask(chess.PAWN, not color) & chess.BB_FILE_D
            and not _passed_pawns(board, color) & d_pawn
        ):
            return color
    return None


def _minority_side(board: chess.Board) -> chess.Color | None:
    """Carlsbad shape: the side with a+b pawns against a+b+c, d-pawns locked,
    equal pawn totals."""
    white = board.pieces_mask(chess.PAWN, chess.WHITE)
    black = board.pieces_mask(chess.PAWN, chess.BLACK)
    if chess.popcount(white) != chess.popcount(black) or not _locked(board) & chess.BB_FILE_D:
        return None
    for color, own, enemy in ((chess.WHITE, white, black), (chess.BLACK, black, white)):
        if (
            all(_single(own & f) for f in _MINORITY_FILES)
            and not own & chess.BB_FILE_C
            and all(_single(enemy & f) for f in _MAJORITY_FILES)
        ):
            return color
    return None


def imbalance_tag(board: chess.Board, our_color: chess.Color) -> str | None:
    # Exclusive by construction (IQP needs an empty enemy d-file); IQP first.
    iqp = _iqp_side(board)
    if iqp is not None:
        return _pov(iqp, our_color, IMBALANCE_IQP_OURS, IMBALANCE_IQP_THEIRS)
    return _pov(
        _minority_side(board), our_color, IMBALANCE_MINORITY_OURS, IMBALANCE_MINORITY_THEIRS,
    )


def _both_queens(board: chess.Board) -> bool:
    return all(board.pieces_mask(chess.QUEEN, color) for color in chess.COLORS)


def bishops_tag(board: chess.Board) -> str | None:
    """One bishop each on opposite-colored squares, no knights on the board
    (a knight can trade itself for a bishop)."""
    white = board.pieces_mask(chess.BISHOP, chess.WHITE)
    black = board.pieces_mask(chess.BISHOP, chess.BLACK)
    if board.knights or not (_single(white) and _single(black)):
        return None
    if bool(white & chess.BB_LIGHT_SQUARES) == bool(black & chess.BB_LIGHT_SQUARES):
        return None
    return BISHOPS_OPPOSITE_QUEENS if _both_queens(board) else BISHOPS_OPPOSITE


def pawn_tag(board: chess.Board, our_color: chess.Color) -> str | None:
    """Outside passer for exactly one side; both is a race, no tag."""
    white = bool(_passed_pawns(board, chess.WHITE) & _OUTSIDE_FILES)
    black = bool(_passed_pawns(board, chess.BLACK) & _OUTSIDE_FILES)
    if white == black:
        return None
    side = chess.WHITE if white else chess.BLACK
    return _pov(side, our_color, PAWN_PASSED_OUTSIDE_OURS, PAWN_PASSED_OUTSIDE_THEIRS)


def castling_tag(board: chess.Board) -> str | None:
    """Kings sheltered on opposite wings, both queens on. King squares only:
    an artificially castled king plays the same race."""
    if not _both_queens(board):
        return None
    white = board.pieces_mask(chess.KING, chess.WHITE) & _WHITE_SHELTER_RANKS
    black = board.pieces_mask(chess.KING, chess.BLACK) & _BLACK_SHELTER_RANKS
    if (white & _QUEENSIDE_KING_FILES and black & _KINGSIDE_KING_FILES) or (
        white & _KINGSIDE_KING_FILES and black & _QUEENSIDE_KING_FILES
    ):
        return CASTLING_OPPOSITE
    return None


def _middlegame_talk(predicate: Callable[[MatchContext], str | None]):
    """Few pawns make every file "open"; structure advice is middlegame talk."""
    return lambda ctx: None if ctx.phase == PHASE_ENDGAME else predicate(ctx)


# Bishops and the outside passer matter in every phase.
REGISTRY: tuple[Matcher, ...] = (
    PredicateMatcher(
        AXIS_STRUCTURE,
        {AXIS_STRUCTURE: frozenset({STRUCTURE_OPEN, STRUCTURE_CLOSED})},
        _middlegame_talk(lambda ctx: structure_tag(ctx.board)),
    ),
    PredicateMatcher(
        AXIS_IMBALANCE,
        {AXIS_IMBALANCE: frozenset({
            IMBALANCE_IQP_OURS, IMBALANCE_IQP_THEIRS,
            IMBALANCE_MINORITY_OURS, IMBALANCE_MINORITY_THEIRS,
        })},
        _middlegame_talk(lambda ctx: imbalance_tag(ctx.board, ctx.our_color)),
    ),
    PredicateMatcher(
        AXIS_BISHOPS,
        {AXIS_BISHOPS: frozenset({BISHOPS_OPPOSITE, BISHOPS_OPPOSITE_QUEENS})},
        lambda ctx: bishops_tag(ctx.board),
    ),
    PredicateMatcher(
        AXIS_PAWN,
        {AXIS_PAWN: frozenset({PAWN_PASSED_OUTSIDE_OURS, PAWN_PASSED_OUTSIDE_THEIRS})},
        lambda ctx: pawn_tag(ctx.board, ctx.our_color),
    ),
    PredicateMatcher(
        AXIS_CASTLING,
        {AXIS_CASTLING: frozenset({CASTLING_OPPOSITE})},
        _middlegame_talk(lambda ctx: castling_tag(ctx.board)),
    ),
)

# Specific-tag axes in render order; structure renders by its own rule.
SPECIFIC_AXES = tuple(
    axis for matcher in REGISTRY for axis in matcher.tags if axis != AXIS_STRUCTURE
)
