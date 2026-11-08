"""Speculative sacrifices on the live board (docs/ai-playbook-spec.md,
Speculative sacrifices) -- pure functions, no engine.

Like `repeating_moves`, not a matcher: the result names concrete moves.
The first stage keeps forcing patterns only, whose compensation a deeper
search can show: two named ones, and a generic king-zone trigger backed
by a one-ply follow-up (a check or a lift). Squares are written from White's side and mirrored for
Black.
"""
from __future__ import annotations

import chess

from ..env_utils import env_int


PATTERN_GREEK_GIFT = "greek_gift"
# Bishop takes the f-pawn next to an uncastled king (Bxf7+ / ...Bxf2+).
PATTERN_BISHOP_F7 = "bishop_f7"
# Any losing capture next to the enemy king that a follow-up backs.
PATTERN_KING_ZONE = "king_zone"

# King-zone sacrifices listed per turn, worst exchange first.
_DEFAULT_KING_ZONE_MAX = 2
KING_ZONE_MAX = env_int("SV_AI_SAC_KING_ZONE_MAX", _DEFAULT_KING_ZONE_MAX)
_HEAVY_PIECES = (chess.ROOK, chess.QUEEN)
# (bishop target, enemy king square) of the named patterns, White's side.
_NAMED_SHAPES = ((chess.H7, chess.G8), (chess.F7, chess.E8))
_SLIDERS = (chess.BISHOP, chess.ROOK, chess.QUEEN)

# Centipawns per piece type; the king outweighs any exchange.
PIECE_VALUES_CP = {
    chess.PAWN: 100,
    chess.KNIGHT: 300,
    chess.BISHOP: 300,
    chess.ROOK: 500,
    chess.QUEEN: 900,
}
_KING_VALUE_CP = 100_000


def _value(piece_type: chess.PieceType) -> int:
    return PIECE_VALUES_CP.get(piece_type, _KING_VALUE_CP)


def _least_valuable(board: chess.Board, attackers: chess.Bitboard) -> chess.Square:
    return min(chess.scan_forward(attackers), key=lambda sq: _value(board.piece_type_at(sq)))


def static_exchange(board: chess.Board, move: chess.Move) -> int:
    """Net centipawns for the mover of the capture sequence on the target
    square, each side recapturing with its least valuable piece and free
    to stop. A king never recaptures onto a defended square."""
    target = move.to_square
    captured = chess.PAWN if board.is_en_passant(move) else board.piece_type_at(target)
    gains = [_value(captured) if captured else 0]
    occupied = board.occupied & ~chess.BB_SQUARES[move.from_square]
    on_square = _value(board.piece_type_at(move.from_square))
    side = not board.turn
    while True:
        attackers = board.attackers_mask(side, target, occupied) & occupied
        if not attackers:
            break
        square = _least_valuable(board, attackers)
        occupied &= ~chess.BB_SQUARES[square]
        if board.piece_type_at(square) == chess.KING and (
            board.attackers_mask(not side, target, occupied) & occupied
        ):
            break
        gains.append(on_square - gains[-1])
        on_square = _value(board.piece_type_at(square))
        side = not side
    for depth in range(len(gains) - 1, 0, -1):
        gains[depth - 1] = -max(-gains[depth - 1], gains[depth])
    return gains[0]


def _relative(square: chess.Square, color: chess.Color) -> chess.Square:
    return square if color == chess.WHITE else chess.square_mirror(square)


def _bishop_checks_onto(board: chess.Board, target: chess.Square) -> list[chess.Move]:
    """The side to move's legal bishop captures of a pawn on `target` that
    give check and lose material by static exchange."""
    if board.piece_at(target) != chess.Piece(chess.PAWN, not board.turn):
        return []
    return [
        move for move in board.legal_moves
        if move.to_square == target
        and board.piece_type_at(move.from_square) == chess.BISHOP
        and board.gives_check(move)
        and static_exchange(board, move) < 0
    ]


def _queen_eyes_h7(board: chess.Board, color: chess.Color) -> bool:
    """A queen of `color` reaches an empty h-file square with a clear file
    up to h7 (h1 behind the h2 pawn does not count)."""
    h7 = _relative(chess.H7, color)
    landing = chess.BB_FILE_H & ~board.occupied
    return any(
        not chess.between(square, h7) & board.occupied
        for queen in board.pieces(chess.QUEEN, color)
        for square in chess.scan_forward(board.attacks_mask(queen) & landing)
    )


def _wins_the_knight(board: chess.Board, square: chess.Square) -> bool:
    """The side to move has a capture on `square` that gains by static
    exchange."""
    return any(
        move.to_square == square and static_exchange(board, move) > 0
        for move in board.legal_moves
    )


def _knight_check_on_g5(board: chess.Board, us: chess.Color) -> bool:
    """After Bxh7+ Kxh7: a knight checks from g5, no capture there wins it,
    and a queen then eyes h7 along the h-file."""
    g5 = _relative(chess.G5, us)
    for move in board.legal_moves:
        if move.to_square != g5 or board.piece_type_at(move.from_square) != chess.KNIGHT:
            continue
        if not board.gives_check(move):
            continue
        after = board.copy(stack=False)
        after.push(move)
        if not _wins_the_knight(after, g5) and _queen_eyes_h7(after, us):
            return True
    return False


def _greek_gift(board: chess.Board) -> list[chess.Move]:
    """Bxh7+ against a king on g8, h7's only defender, that takes back;
    then Ng5+ and a queen to the h-file."""
    us = board.turn
    king = board.king(not us)
    h7 = _relative(chess.H7, us)
    if king != _relative(chess.G8, us):
        return []
    out = []
    for move in _bishop_checks_onto(board, h7):
        after = board.copy(stack=False)
        after.push(move)
        recapture = chess.Move(king, h7)
        if after.attackers_mask(not us, h7) != chess.BB_SQUARES[king] or not after.is_legal(recapture):
            continue
        after.push(recapture)
        if _knight_check_on_g5(after, us):
            out.append(move)
    return out


def _bishop_f7(board: chess.Board) -> list[chess.Move]:
    """Bxf7+ against a king still on e8."""
    us = board.turn
    if board.king(not us) != _relative(chess.E8, us):
        return []
    return _bishop_checks_onto(board, _relative(chess.F7, us))


def _king_zone_mask(board: chess.Board, color: chess.Color) -> chess.Bitboard:
    """`color`'s king square and its neighbors."""
    king = board.king(color)
    return chess.BB_EMPTY if king is None else chess.BB_KING_ATTACKS[king] | chess.BB_SQUARES[king]


def _has_check(board: chess.Board) -> bool:
    """The side to move has a check that does not lose the checking piece:
    by a rook or queen, or landing next to the enemy king."""
    zone = _king_zone_mask(board, not board.turn)
    return any(
        board.gives_check(move)
        and (board.piece_type_at(move.from_square) in _HEAVY_PIECES
             or chess.BB_SQUARES[move.to_square] & zone)
        and static_exchange(board, move) >= 0
        for move in board.legal_moves
    )


def _has_lift(before: chess.Board, after: chess.Board, target: chess.Square) -> bool:
    """A line piece of the side to move in `after` attacks a square next to
    the enemy king that it did not attack in `before`: a file or diagonal
    opened onto the king. The sacrifice square `target` does not count: a
    line onto it only hits the recapturing piece."""
    us = after.turn
    zone = _king_zone_mask(after, not us) & ~chess.BB_SQUARES[target]
    for piece_type in _SLIDERS:
        for square in after.pieces(piece_type, us):
            was = before.attacks_mask(square) if before.piece_at(square) == after.piece_at(square) else 0
            if after.attacks_mask(square) & zone & ~was:
                return True
    return False


def _king_zone(board: chess.Board) -> list[chess.Move]:
    """Captures next to the enemy king that lose material by static
    exchange, where every legal recapture leaves a check or a lift."""
    zone = _king_zone_mask(board, not board.turn)
    found: list[tuple[int, chess.Move]] = []
    for move in board.legal_moves:
        if not (board.is_capture(move) and chess.BB_SQUARES[move.to_square] & zone):
            continue
        if board.piece_type_at(move.from_square) == chess.KING or _named_shape(board, move):
            continue
        exchange = static_exchange(board, move)
        if exchange >= 0:
            continue
        after = board.copy(stack=False)
        after.push(move)
        recaptures = [m for m in after.legal_moves if m.to_square == move.to_square]
        if recaptures and all(_follows_up(board, after, r) for r in recaptures):
            found.append((exchange, move))
    found.sort(key=lambda pair: pair[0])
    return [move for _, move in found[:KING_ZONE_MAX]]


def _follows_up(before: chess.Board, after: chess.Board, recapture: chess.Move) -> bool:
    board = after.copy(stack=False)
    board.push(recapture)
    return _has_check(board) or _has_lift(before, board, recapture.to_square)


def _named_shape(board: chess.Board, move: chess.Move) -> bool:
    """`move` has a named pattern's shape (a bishop onto h7 against a king
    on g8, onto f7 against one on e8): that pattern decides it, even when
    it rejects the move."""
    us = board.turn
    if board.piece_type_at(move.from_square) != chess.BISHOP:
        return False
    king = board.king(not us)
    return any(
        move.to_square == _relative(target, us) and king == _relative(king_square, us)
        for target, king_square in _NAMED_SHAPES
    )


_NAMED_PATTERNS = (
    (PATTERN_GREEK_GIFT, _greek_gift),
    (PATTERN_BISHOP_F7, _bishop_f7),
)


def sacrifice_moves(board: chess.Board) -> tuple[tuple[str, str], ...]:
    """(pattern, SAN) for each sacrifice the side to move has: the named
    patterns first, then king-zone ones they do not already name."""
    named = [(name, move) for name, detect in _NAMED_PATTERNS for move in detect(board)]
    seen = {move for _, move in named}
    generic = [(PATTERN_KING_ZONE, move) for move in _king_zone(board) if move not in seen]
    return tuple((name, board.san(move)) for name, move in named + generic)
