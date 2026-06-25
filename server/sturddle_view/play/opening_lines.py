"""Opening-line book for HVE: pick an opening line for a new game.

Mirrors the fastchess ``-openings`` format inference (fastchess.py): an
``.epd`` file is one FEN per line; any other extension is PGN. Selection
honors the book order: ``random`` picks a line per game; ``sequential``
walks the lines via a caller-supplied cursor (modulo the line count).

The two book kinds drive HVE differently. EPD yields a ``start_fen`` --
the game starts from that position (works for an engine playing white).
PGN yields the line's UCI moves (clamped to ``plies``); HVE follows that
line, playing the engine's book moves while the human stays on it, and
falls out of book on the first deviation.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

import chess

from ..config import BOOK_ORDER_RANDOM

# Extension that marks a position book (one FEN per line); anything else
# is parsed as PGN. Matches fastchess format inference in fastchess.py.
_EPD_EXT = ".epd"

# Book index cache keyed by file stat. The index is the cheap part -- a
# list of per-line raw entries (EPD: a FEN each; PGN: a movetext chunk
# each) -- with NO move parsing. Only the single selected line's moves are
# parsed per new game (see select_seed), so book size doesn't gate latency.
# Invalidated when the file's mtime/size changes.
_BookKey = tuple[str, int, int]
# (is_epd, entries): EPD entries are FEN strings, PGN entries are movetext.
_index_cache: dict[_BookKey, tuple[bool, list[str]]] = {}

# Movetext cleanup so the cheap tokenizer below sees only SAN tokens: drop
# brace comments, $N NAGs, and the result token. Parenthesized variations
# are stripped separately (nesting-aware).
_PGN_COMMENT = re.compile(r"\{[^}]*\}")
_PGN_NAG = re.compile(r"\$\d+")
_PGN_RESULTS = {"*", "1-0", "0-1", "1/2-1/2"}
# Split a multi-game PGN on the blank-line gap before a new game's headers.
_PGN_GAME_SPLIT = re.compile(r"\n\s*\n(?=\[)")


@dataclass(slots=True, frozen=True)
class OpeningSeed:
    """A selected opening line. EPD books set ``start_fen`` (start position);
    PGN books set ``moves_uci`` (the line the engine follows)."""

    start_fen: str | None = None
    moves_uci: tuple[str, ...] = ()


# When the book sets no ply cap, parse this many plies for the selected
# line. A book line never needs to run deeper.
_DEFAULT_PARSE_PLIES = 20


def _epd_to_fen(entry: str) -> str | None:
    """Full FEN for one EPD line, or None if it doesn't parse as a board."""
    board = chess.Board()
    try:
        board.set_epd(entry.strip())
    except ValueError:
        return None
    return board.fen()


def _pgn_movetext(rec: str) -> str:
    """The movetext of one PGN game chunk (header lines dropped)."""
    return " ".join(l for l in rec.splitlines() if not l.startswith("["))


def _build_index(path: Path) -> tuple[bool, list[str]]:
    """Cheap pass over a book file: split it into per-line raw entries
    WITHOUT parsing moves. EPD -> (True, [fen-line, ...]); PGN -> (False,
    [game-chunk, ...]). Only blank entries are dropped here; validity is
    checked when a line is actually selected."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == _EPD_EXT:
        return True, [ln for ln in (s.strip() for s in text.splitlines()) if ln]
    return False, [rec for rec in _PGN_GAME_SPLIT.split(text) if rec.strip()]


def _load_index(path: Path) -> tuple[bool, list[str]]:
    """Cached _build_index, keyed by file stat. This is the only full-file
    pass; it does no move parsing, so it stays sub-second on large books."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _index_cache.get(key)
    if cached is None:
        cached = _build_index(path)
        _index_cache[key] = cached
    return cached


def _strip_variations(movetext: str) -> str:
    """Drop parenthesized sidelines (nesting-aware). A regex can't match
    balanced nesting, so scan and skip while depth > 0."""
    out: list[str] = []
    depth = 0
    for ch in movetext:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _line_moves(movetext: str, plies: int) -> tuple[str, ...]:
    """First ``plies`` UCI moves of one game's movetext. SAN is converted
    on a fresh board (so legality is real), stopping at the cap or the
    first unparseable token -- which truncates a line rather than dropping
    it. Comments / NAGs / variations are removed before tokenizing."""
    cleaned = _strip_variations(_PGN_NAG.sub(" ", _PGN_COMMENT.sub(" ", movetext)))
    board = chess.Board()
    moves: list[str] = []
    for tok in cleaned.split():
        if len(moves) >= plies:
            break
        if tok in _PGN_RESULTS:
            continue
        # Strip move numbers ("1." / "1..." / "12.e4") to bare SAN.
        tok = tok.split(".")[-1]
        if not tok or tok[0].isdigit():
            continue
        try:
            move = board.parse_san(tok)
        except ValueError:
            break
        board.push(move)
        moves.append(move.uci())
    return tuple(moves)


def select_seed(
    book_path: str,
    plies: int | None,
    order: str | None,
    cursor: int,
) -> OpeningSeed | None:
    """Pick one opening line from the book at ``book_path``.

    Indexes the file once (cached, no move parsing) and parses ONLY the
    single selected line, so latency is independent of book size.
    ``random`` order picks a uniform line; any other order is sequential
    (``cursor % line_count``). The chosen line is searched outward from
    its index for the first usable entry, so a malformed line doesn't
    abort selection. PGN lines are capped at ``plies`` (None ->
    _DEFAULT_PARSE_PLIES). Returns None if the file is missing/empty."""
    path = Path(book_path)
    if not path.is_file():
        return None
    is_epd, entries = _load_index(path)
    if not entries:
        return None
    cap = plies if plies is not None else _DEFAULT_PARSE_PLIES
    start = _pick_index(len(entries), order, cursor)
    # Probe from the picked index forward (wrapping) for a line that
    # parses -- one bad EPD/PGN line shouldn't yield "no opening".
    for off in range(len(entries)):
        entry = entries[(start + off) % len(entries)]
        if is_epd:
            fen = _epd_to_fen(entry)
            if fen is not None:
                return OpeningSeed(start_fen=fen)
        else:
            moves = _line_moves(_pgn_movetext(entry), cap)
            if moves:
                return OpeningSeed(moves_uci=moves)
    return None


def _pick_index(count: int, order: str | None, cursor: int) -> int:
    if order == BOOK_ORDER_RANDOM:
        return random.randrange(count)
    return cursor % count
