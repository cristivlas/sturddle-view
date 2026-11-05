"""Situation classifier for the AI playbook (docs/ai-playbook-spec.md).

Pure function of (board, eval, our side) -> Situation tags. No engine,
no LLM. Margin and phase are the base tags, computed here; the other
tags come from the matchers (play/matchers.py), which read them.
Thresholds are named constants with SV_ env overrides. The tags drive
the pick: the plan steer the narrator reads, the recommend_move
gate's margin and its repetition veto.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import chess

from ..chess.score import SCORE_CP, SCORE_MATE
from ..env_utils import env_int
from .matchers import REGISTRY, Match, MatchContext, Matcher
from .playbook_tags import (
    MARGIN_BETTER,
    MARGIN_CRUSHING,
    MARGIN_EVEN,
    MARGIN_LOSING,
    MARGIN_LOST,
    MARGIN_WINNING,
    MARGIN_WORSE,
    PHASE_ENDGAME,
    PHASE_LATE,
    PHASE_MIDDLEGAME,
    PHASE_OPENING,
    SOURCE_EVAL,
    SOURCE_MATERIAL,
)
from .sacrifices import PIECE_VALUES_CP, sacrifice_moves


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


@dataclass(frozen=True, slots=True)
class Situation:
    margin: str
    margin_source: str
    phase: str
    structure: str | None = None
    # SAN of the legal moves that repeat an earlier position of the game.
    repeats: tuple[str, ...] = ()
    # (pattern, SAN) of the known sacrifices our side has, when on move.
    sacrifices: tuple[tuple[str, str], ...] = ()
    imbalance: str | None = None
    bishops: str | None = None
    pawn: str | None = None
    castling: str | None = None
    formation: str | None = None
    # Every match, in matcher order.
    matches: tuple[Match, ...] = ()


def _material_cp_white(board: chess.Board) -> int:
    total = 0
    # Material proxy when no eval is at hand.
    for piece_type, value in PIECE_VALUES_CP.items():
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
    mate = score.get(SCORE_MATE)
    if isinstance(mate, int) and mate != 0:
        return RESIGN_CP if mate > 0 else -RESIGN_CP
    cp = score.get(SCORE_CP)
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


def repeating_moves(board: chess.Board) -> tuple[str, ...]:
    """SAN of the legal moves after which the position has occurred before
    in the game. Needs the move stack; a bare FEN yields none."""
    if not board.move_stack:
        return ()
    scratch = board.copy()
    out: list[str] = []
    for move in scratch.legal_moves:
        san = scratch.san(move)
        scratch.push(move)
        if scratch.is_repetition(2):
            out.append(san)
        scratch.pop()
    return tuple(out)


def classify(
    board: chess.Board,
    *,
    score_white: dict | None,
    our_color: chess.Color,
    matchers: Sequence[Matcher] = REGISTRY,
) -> Situation:
    """Tags for `board` from `our_color`'s point of view. `score_white` is
    an engine/PGN eval dict ({cp} or {mate}, white POV); None or unusable
    falls back to the material balance. Repetitions come from the board's
    move stack. Sacrifices are the side to move's, so they are listed only
    when that is `our_color`: the margin must be the mover's. `matchers`
    defaults to the static registry; the catalog's composed registry adds
    the data-defined ones."""
    cp_white = _score_cp_white(score_white)
    source = SOURCE_EVAL
    if cp_white is None:
        cp_white = _material_cp_white(board)
        source = SOURCE_MATERIAL
    cp_ours = cp_white if our_color == chess.WHITE else -cp_white
    ctx = MatchContext(
        board=board, our_color=our_color, margin=_margin_tag(cp_ours),
        margin_source=source, phase=phase_tag(board),
    )
    matches = tuple(m for matcher in matchers for m in matcher.match(ctx))
    return Situation(
        margin=ctx.margin,
        margin_source=source,
        phase=ctx.phase,
        repeats=repeating_moves(board),
        sacrifices=sacrifice_moves(board) if board.turn == our_color else (),
        matches=matches,
        **{m.axis: m.tag for m in matches},
    )
