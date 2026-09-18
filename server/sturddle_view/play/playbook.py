"""Situation classifier for the AI playbook (docs/ai-playbook-spec.md).

Pure function of (board, eval, our side) -> Situation tags. No engine,
no LLM. Thresholds are named constants with SV_ env overrides.
"""
from __future__ import annotations

from dataclasses import dataclass

import chess

from ..env_utils import env_int


# Margin buckets, centipawns from our side's point of view.
_DEFAULT_EDGE_CP = 100
_DEFAULT_DECISIVE_CP = 300
_DEFAULT_RESIGN_CP = 900
EDGE_CP = env_int("SV_AI_PLAYBOOK_EDGE_CP", _DEFAULT_EDGE_CP)
DECISIVE_CP = env_int("SV_AI_PLAYBOOK_DECISIVE_CP", _DEFAULT_DECISIVE_CP)
RESIGN_CP = env_int("SV_AI_PLAYBOOK_RESIGN_CP", _DEFAULT_RESIGN_CP)

# Phase by pawn count, buckets of 4 (as in the engine): the lower pawn
# count that still counts as the bucket.
_DEFAULT_OPENING_MIN_PAWNS = 13
_DEFAULT_MIDDLEGAME_MIN_PAWNS = 9
_DEFAULT_LATE_MIN_PAWNS = 5
OPENING_MIN_PAWNS = env_int("SV_AI_PLAYBOOK_OPENING_MIN_PAWNS", _DEFAULT_OPENING_MIN_PAWNS)
MIDDLEGAME_MIN_PAWNS = env_int("SV_AI_PLAYBOOK_MIDDLEGAME_MIN_PAWNS", _DEFAULT_MIDDLEGAME_MIN_PAWNS)
LATE_MIN_PAWNS = env_int("SV_AI_PLAYBOOK_LATE_MIN_PAWNS", _DEFAULT_LATE_MIN_PAWNS)

# Structure: locked pawn pairs and open / half-open files.
_DEFAULT_CLOSED_MIN_LOCKED = 3
_DEFAULT_CLOSED_MAX_OPEN_FILES = 1
_DEFAULT_OPEN_MIN_FILES = 4
CLOSED_MIN_LOCKED = env_int("SV_AI_PLAYBOOK_CLOSED_MIN_LOCKED", _DEFAULT_CLOSED_MIN_LOCKED)
CLOSED_MAX_OPEN_FILES = env_int("SV_AI_PLAYBOOK_CLOSED_MAX_OPEN_FILES", _DEFAULT_CLOSED_MAX_OPEN_FILES)
OPEN_MIN_FILES = env_int("SV_AI_PLAYBOOK_OPEN_MIN_FILES", _DEFAULT_OPEN_MIN_FILES)

# Material proxy when no eval is at hand (centipawns per piece type).
_PIECE_VALUES_CP = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 300,
    chess.ROOK: 500,
    chess.QUEEN: 900,
}

# Tag vocabulary. Strings so they render into prompt keys directly.
MARGIN_LOST = "lost"
MARGIN_LOSING = "losing"
MARGIN_WORSE = "worse"
MARGIN_EVEN = "even"
MARGIN_BETTER = "better"
MARGIN_WINNING = "winning"
MARGIN_CRUSHING = "crushing"

PHASE_OPENING = "opening"
PHASE_MIDDLEGAME = "middlegame"
PHASE_LATE = "late"
PHASE_ENDGAME = "endgame"

STRUCTURE_OPEN = "open"
STRUCTURE_CLOSED = "closed"

SOURCE_EVAL = "eval"
SOURCE_MATERIAL = "material"

# Eval dict keys, as the engine-info schema and PGN import write them.
_SCORE_CP = "cp"
_SCORE_MATE = "mate"


@dataclass(frozen=True, slots=True)
class Situation:
    margin: str
    margin_source: str
    phase: str
    structure: str | None


def _material_cp_white(board: chess.Board) -> int:
    total = 0
    for piece_type, value in _PIECE_VALUES_CP.items():
        total += value * (
            len(board.pieces(piece_type, chess.WHITE))
            - len(board.pieces(piece_type, chess.BLACK))
        )
    return total


def _score_cp_white(score: dict | None) -> int | None:
    """White-POV centipawns from an eval dict; a mate maps past the resign
    threshold on the mating side. None when the dict carries no usable
    score (mate 0 is a finished game, not a margin)."""
    if not score:
        return None
    mate = score.get(_SCORE_MATE)
    if isinstance(mate, int) and mate != 0:
        return RESIGN_CP if mate > 0 else -RESIGN_CP
    cp = score.get(_SCORE_CP)
    return cp if isinstance(cp, int) else None


def _margin_tag(cp_ours: int) -> str:
    if cp_ours <= -RESIGN_CP:
        return MARGIN_LOST
    if cp_ours <= -DECISIVE_CP:
        return MARGIN_LOSING
    if cp_ours <= -EDGE_CP:
        return MARGIN_WORSE
    if cp_ours >= RESIGN_CP:
        return MARGIN_CRUSHING
    if cp_ours >= DECISIVE_CP:
        return MARGIN_WINNING
    if cp_ours >= EDGE_CP:
        return MARGIN_BETTER
    return MARGIN_EVEN


def phase_tag(board: chess.Board) -> str:
    pawns = len(board.pieces(chess.PAWN, chess.WHITE)) + len(board.pieces(chess.PAWN, chess.BLACK))
    if pawns >= OPENING_MIN_PAWNS:
        return PHASE_OPENING
    if pawns >= MIDDLEGAME_MIN_PAWNS:
        return PHASE_MIDDLEGAME
    if pawns >= LATE_MIN_PAWNS:
        return PHASE_LATE
    return PHASE_ENDGAME


def _locked_pairs(board: chess.Board) -> int:
    """White pawns with a black pawn directly in front of them."""
    white = board.pieces(chess.PAWN, chess.WHITE)
    black = board.pieces(chess.PAWN, chess.BLACK)
    return sum(1 for sq in white if sq + 8 in black)


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


def classify(
    board: chess.Board, *, score_white: dict | None, our_color: chess.Color,
) -> Situation:
    """Tags for `board` from `our_color`'s point of view. `score_white` is
    an engine/PGN eval dict ({cp} or {mate}, white POV); None or unusable
    falls back to the material balance."""
    cp_white = _score_cp_white(score_white)
    source = SOURCE_EVAL
    if cp_white is None:
        cp_white = _material_cp_white(board)
        source = SOURCE_MATERIAL
    cp_ours = cp_white if our_color == chess.WHITE else -cp_white
    phase = phase_tag(board)
    # Few pawns make every file "open"; structure advice is middlegame talk.
    structure = None if phase == PHASE_ENDGAME else structure_tag(board)
    return Situation(
        margin=_margin_tag(cp_ours),
        margin_source=source,
        phase=phase,
        structure=structure,
    )
