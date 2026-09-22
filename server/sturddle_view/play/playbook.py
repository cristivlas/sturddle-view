"""Situation classifier for the AI playbook (docs/ai-playbook-spec.md).

Pure function of (board, eval, our side) -> Situation tags. No engine,
no LLM. Thresholds are named constants with SV_ env overrides. The tags
drive the pick: the plan steer the narrator reads, the recommend_move
gate's margin and its repetition veto.
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
CLOSED_MAX_OPEN_FILES = env_int(
    "SV_AI_PLAYBOOK_CLOSED_MAX_OPEN_FILES", _DEFAULT_CLOSED_MAX_OPEN_FILES,
)
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
AHEAD_MARGINS = frozenset({MARGIN_BETTER, MARGIN_WINNING, MARGIN_CRUSHING})
BEHIND_MARGINS = frozenset({MARGIN_WORSE, MARGIN_LOSING, MARGIN_LOST})

PHASE_OPENING = "opening"
PHASE_MIDDLEGAME = "middlegame"
PHASE_LATE = "late"
PHASE_ENDGAME = "endgame"

STRUCTURE_OPEN = "open"
STRUCTURE_CLOSED = "closed"

SOURCE_EVAL = "eval"
SOURCE_MATERIAL = "material"

# Specific tags. `_ours` / `_theirs` take our side's point of view.
_OPPOSITE = "opposite"
IMBALANCE_IQP_OURS = "iqp_ours"
IMBALANCE_IQP_THEIRS = "iqp_theirs"
IMBALANCE_MINORITY_OURS = "minority_ours"
IMBALANCE_MINORITY_THEIRS = "minority_theirs"
BISHOPS_OPPOSITE_QUEENS = "opposite_queens"
BISHOPS_OPPOSITE = _OPPOSITE
PAWN_PASSED_OUTSIDE_OURS = "passed_outside_ours"
PAWN_PASSED_OUTSIDE_THEIRS = "passed_outside_theirs"
CASTLING_OPPOSITE = _OPPOSITE

# Board geometry for the specific-tag predicates.
_IQP_NEIGHBOR_FILES = chess.BB_FILE_C | chess.BB_FILE_E
_MINORITY_FILES = (chess.BB_FILE_A, chess.BB_FILE_B)
_MAJORITY_FILES = (*_MINORITY_FILES, chess.BB_FILE_C)
_OUTSIDE_FILES = chess.BB_FILE_A | chess.BB_FILE_B | chess.BB_FILE_G | chess.BB_FILE_H
_QUEENSIDE_KING_FILES = chess.BB_FILE_A | chess.BB_FILE_B | chess.BB_FILE_C
_KINGSIDE_KING_FILES = chess.BB_FILE_G | chess.BB_FILE_H
_WHITE_SHELTER_RANKS = chess.BB_RANK_1 | chess.BB_RANK_2
_BLACK_SHELTER_RANKS = chess.BB_RANK_7 | chess.BB_RANK_8

# Eval dict keys, as the engine-info schema and PGN import write them.
_SCORE_CP = "cp"
_SCORE_MATE = "mate"


@dataclass(frozen=True, slots=True)
class Situation:
    margin: str
    margin_source: str
    phase: str
    structure: str | None
    # SAN of the legal moves that repeat an earlier position of the game.
    repeats: tuple[str, ...] = ()
    imbalance: str | None = None
    bishops: str | None = None
    pawn: str | None = None
    castling: str | None = None


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
    board: chess.Board, *, score_white: dict | None, our_color: chess.Color,
) -> Situation:
    """Tags for `board` from `our_color`'s point of view. `score_white` is
    an engine/PGN eval dict ({cp} or {mate}, white POV); None or unusable
    falls back to the material balance. Repetitions come from the board's
    move stack."""
    cp_white = _score_cp_white(score_white)
    source = SOURCE_EVAL
    if cp_white is None:
        cp_white = _material_cp_white(board)
        source = SOURCE_MATERIAL
    cp_ours = cp_white if our_color == chess.WHITE else -cp_white
    phase = phase_tag(board)
    # Few pawns make every file "open"; structure advice is middlegame talk.
    # Bishops and the outside passer matter in every phase.
    middlegame_talk = phase != PHASE_ENDGAME
    return Situation(
        margin=_margin_tag(cp_ours),
        margin_source=source,
        phase=phase,
        structure=structure_tag(board) if middlegame_talk else None,
        repeats=repeating_moves(board),
        imbalance=imbalance_tag(board, our_color) if middlegame_talk else None,
        bishops=bishops_tag(board),
        pawn=pawn_tag(board, our_color),
        castling=castling_tag(board) if middlegame_talk else None,
    )
