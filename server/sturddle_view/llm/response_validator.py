"""Server-side validation of model prose against the live position.

Two pure functions over (text, board) -> list[str]:

- `find_illegal_moves`: SAN-shaped tokens in the prose that fail
  board.parse_san() (piece moves, pawn captures, castling).
- `find_false_piece_claims`: "<piece> on <square>" phrases whose
  square does not actually hold that piece in the live position.

Empty list means the prose is consistent. The coordinator combines
both and injects a corrective user message when either fires.
"""
from __future__ import annotations

import re

import chess


# SAN-like token recognizer. Captures piece moves (Nf3, Qxh7+, R1d2),
# castles (O-O, O-O-O), pawn captures (exd5, exd1=N+), and trailing
# annotation glyphs (!, ?, !?, ?!, !!, ??).
#
# Bare-square pawn moves (e4, h6, ...) are intentionally NOT matched:
# chess prose mentions squares constantly ("the h6 pawn", "targets g7"),
# and a bare square has no syntactic marker to distinguish description
# from move. Loss: cannot flag a model that writes "play e5" when e5
# is illegal. Mitigation: prompt the model to name pawn moves with
# capture notation or a piece-style cue when feasible. Same scoping
# convention python-chess and PGN parsers use.
#
# Glyphs are inside the captured group so they survive to the return
# value (the corrective message echoes them). Intentionally over-accepts:
# parse_san() is the authoritative filter.
_SAN_TOKEN_RE = re.compile(
    r"\b("
    r"(?:O-O-O|O-O"                                       # castling
    r"|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?"  # piece move
    r"|[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?)"                # pawn capture only
    r"[!?]{0,2}"                                          # annotation glyphs
    r")"
)


# Tokens that look like SAN but are not. The parser would reject these
# anyway, but skipping them up front cuts noise in transcripts.
_NON_MOVE_LITERALS = frozenset({
    # Common chess prose that can match piece-move regex.
    # (Add here only when a real-world prose example trips a false positive.)
})


_PIECE_LETTER_TO_TYPE = {
    "K": chess.KING,
    "Q": chess.QUEEN,
    "R": chess.ROOK,
    "B": chess.BISHOP,
    "N": chess.KNIGHT,
}


def _is_san_label(bare: str, board: chess.Board) -> bool:
    """True when `bare` is a 'piece+square' SAN-shape that names a piece
    already on the named square for the side to move -- e.g. 'Qd1' when
    the queen is on d1. Such tokens are labels in prose, not move
    proposals, and should not be flagged as illegal."""
    if len(bare) != 3:
        return False
    piece_char = bare[0]
    if piece_char not in _PIECE_LETTER_TO_TYPE:
        return False
    try:
        square = chess.parse_square(bare[1:3])
    except ValueError:
        return False
    piece = board.piece_at(square)
    if piece is None:
        return False
    if piece.piece_type != _PIECE_LETTER_TO_TYPE[piece_char]:
        return False
    return piece.color == board.turn


def find_illegal_moves(text: str, board: chess.Board) -> list[str]:
    """Return distinct illegal SAN tokens found in `text` (order of first
    appearance, no duplicates). Tokens that don't parse as SAN at all are
    ignored -- only well-formed moves the board rejects count as illegal.

    A SAN-shaped token whose destination already holds the named piece
    for the side to move (e.g. 'Qd1' when the queen IS on d1) is treated
    as a label, not an illegal move.

    Annotation glyphs (!, ?, !?, !!) are stripped before parsing but
    preserved in the returned token so the corrective message echoes
    what the model wrote.
    """
    seen: set[str] = set()
    illegal: list[str] = []
    for match in _SAN_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token in seen or token in _NON_MOVE_LITERALS:
            continue
        seen.add(token)
        bare = _strip_annotation_glyphs(token)
        try:
            board.parse_san(bare)
        except chess.IllegalMoveError:
            if _is_san_label(bare, board):
                continue
            illegal.append(token)
        except (chess.InvalidMoveError, chess.AmbiguousMoveError):
            # Not a move at all, or ambiguous (which is the model's
            # problem to disambiguate, not ours to flag as illegal).
            continue
    return illegal


_GLYPH_RE = re.compile(r"[!?]{1,2}$")


def _strip_annotation_glyphs(token: str) -> str:
    return _GLYPH_RE.sub("", token)


# Natural-language castle mention -- models often write "castle"
# instead of "O-O". False positive risk: "castles" colloquially names
# rook pieces ("Black's castles guard the back rank") -- rare in prose.
_CASTLE_WORD_RE = re.compile(r"\b(castles?|castling|castled)\b", re.IGNORECASE)


def _any_castle_legal(board: chess.Board) -> bool:
    for move in board.legal_moves:
        if board.is_castling(move):
            return True
    # Copy so flipping turn doesn't mutate the caller's board.
    other = board.copy(stack=False)
    other.turn = not other.turn
    for move in other.legal_moves:
        if other.is_castling(move):
            return True
    return False


def find_castle_word_violations(text: str, board: chess.Board) -> list[str]:
    """Return distinct castle-word mentions (e.g. 'castle', 'castling')
    found in `text` when neither side has any legal castling move in
    the live position. Order of first appearance, deduped. Empty when
    castling is legal for at least one side, or when no castle word
    appears in the text.
    """
    matches = list(_CASTLE_WORD_RE.finditer(text))
    if not matches:
        return []
    if _any_castle_legal(board):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        token = m.group(0)
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


# Piece-on-square claim recognizer. Two phrasings, both common in
# chess prose:
#   1. "<piece> on <square>"  -- "the knight on f1", "White's bishop on d2"
#   2. "<square> <piece>"      -- "the b5 pawn", "Black's g1 knight"
# Optional leading color (with or without possessive 's) and optional
# article ("the"). Case-insensitive on words, sensitive on coords.
_PIECE_WORDS = {
    "king":   chess.KING,
    "queen":  chess.QUEEN,
    "rook":   chess.ROOK,
    "bishop": chess.BISHOP,
    "knight": chess.KNIGHT,
    "pawn":   chess.PAWN,
}
_COLOR_WORDS = {"white": chess.WHITE, "black": chess.BLACK}
_PIECE_ALT = "|".join(_PIECE_WORDS)
_COLOR_OPT = r"(?:(white|black)(?:'s)?\s+)?(?:the\s+)?"
# Group layout (positional): (color, piece, square) in both regexes.
_PIECE_ON_SQUARE_RE = re.compile(
    rf"\b{_COLOR_OPT}({_PIECE_ALT})\s+on\s+([a-h][1-8])\b",
    re.IGNORECASE,
)
_SQUARE_PIECE_RE = re.compile(
    rf"\b{_COLOR_OPT}([a-h][1-8])\s+({_PIECE_ALT})\b",
    re.IGNORECASE,
)


def _iter_piece_claims(text: str):
    """Yield (color_word, piece_word, square_name) for every claim
    matched by either phrasing regex. Positional groups: both regexes
    expose (color, piece-or-square, square-or-piece) in a known order;
    we adapt per regex."""
    for m in _PIECE_ON_SQUARE_RE.finditer(text):
        color, piece, square = m.group(1), m.group(2), m.group(3)
        yield (color or "").lower(), piece.lower(), square.lower()
    for m in _SQUARE_PIECE_RE.finditer(text):
        color, square, piece = m.group(1), m.group(2), m.group(3)
        yield (color or "").lower(), piece.lower(), square.lower()


# Bare-square pawn-push token; carve-out-only (bare pushes are still
# excluded from illegal-move detection). Trailing lookahead instead of
# \b so glyphs at the end ('e8=Q+') don't break the boundary.
_BARE_PAWN_PUSH_RE = re.compile(r"\b[a-h][1-8](?:=[QRBN])?[+#]?(?![a-zA-Z0-9])")


def _add_move_target(
    out: set[tuple[int, chess.Color, int]], token: str, board: chess.Board,
) -> None:
    try:
        move = board.parse_san(token)
    except (chess.IllegalMoveError, chess.InvalidMoveError, chess.AmbiguousMoveError):
        return
    piece = board.piece_at(move.from_square)
    if piece is None:
        return
    dest_piece_type = move.promotion if move.promotion else piece.piece_type
    out.add((dest_piece_type, piece.color, move.to_square))


def _move_target_set(text: str, board: chess.Board) -> set[tuple[int, chess.Color, int]]:
    """Scan `text` for tokens that parse as legal moves on `board`;
    return the set of (piece_type, color, dest_square) tuples those
    moves would create on the post-move board. Carves out forward-looking
    prose (post-Re1 "the rook on e1", post-e4 "the e4 pawn") from
    false-piece-claim flagging."""
    out: set[tuple[int, chess.Color, int]] = set()
    for match in _SAN_TOKEN_RE.finditer(text):
        _add_move_target(out, _strip_annotation_glyphs(match.group(0)), board)
    for match in _BARE_PAWN_PUSH_RE.finditer(text):
        # Strip kept for symmetry; _BARE_PAWN_PUSH_RE doesn't capture !?.
        _add_move_target(out, _strip_annotation_glyphs(match.group(0)), board)
    return out


def find_false_piece_claims(text: str, board: chess.Board) -> list[str]:
    """Return distinct false 'piece on square' claims (order of first
    appearance). A claim is false when the named square is empty or
    holds a piece of a different type. When the claim names a color,
    a mismatched color also counts as false. Two phrasings recognized:
    "<piece> on <square>" and "<square> <piece>".

    Claims that match the destination of a legal SAN-shaped move in the
    same text are skipped -- those describe the resulting state of a
    recommendation, not the live position.
    """
    targets = _move_target_set(text, board)
    seen: set[str] = set()
    false: list[str] = []
    for color_word, piece_word, square_name in _iter_piece_claims(text):
        key = f"{color_word}|{piece_word}|{square_name}"
        if key in seen:
            continue
        seen.add(key)
        piece_type = _PIECE_WORDS[piece_word]
        square = chess.parse_square(square_name)
        # Forward-looking carve-out: a legal SAN in the prose places
        # exactly this piece on this square (color must match the move's
        # color, defaulting to the moving side when prose has no color).
        claim_color = _COLOR_WORDS[color_word] if color_word else board.turn
        if (piece_type, claim_color, square) in targets:
            continue
        actual = board.piece_at(square)
        prefix = f"{color_word} " if color_word else ""
        label = f"{prefix}{piece_word} on {square_name}"
        if actual is None or actual.piece_type != piece_type:
            false.append(label)
            continue
        if color_word and actual.color != _COLOR_WORDS[color_word]:
            false.append(label)
    return false
