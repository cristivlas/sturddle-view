"""Speculative sacrifices on the live board (docs/ai-playbook-spec.md,
Speculative sacrifices) -- pure functions, no engine.

Like `repeating_moves`, not a matcher: the result names concrete moves.
The first stage keeps forcing patterns only, whose compensation a deeper
search can show. Squares are written from White's side and mirrored for
Black.
"""
from __future__ import annotations

import chess


PATTERN_GREEK_GIFT = "greek_gift"
# Bishop takes the f-pawn next to an uncastled king (Bxf7+ / ...Bxf2+).
PATTERN_BISHOP_F7 = "bishop_f7"

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


_PATTERNS = (
    (PATTERN_GREEK_GIFT, _greek_gift),
    (PATTERN_BISHOP_F7, _bishop_f7),
)


def sacrifice_moves(board: chess.Board) -> tuple[tuple[str, str], ...]:
    """(pattern, SAN) for each known sacrifice the side to move has, in
    pattern order."""
    return tuple(
        (name, board.san(move))
        for name, detect in _PATTERNS
        for move in detect(board)
    )
