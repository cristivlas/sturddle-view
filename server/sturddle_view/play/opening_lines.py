"""Opening book for HVE: prefix-match played moves against book lines.

Mirrors the fastchess ``-openings`` format inference (fastchess.py): an
``.epd`` file is one FEN per line; any other extension is PGN. The two
book kinds drive HVE differently. EPD yields a start position for the
new game (select_epd_seed). PGN books are consulted on every engine
turn (book_reply): the pool of lines whose opening tokens match the
moves played so far supplies the engine's reply. ``sequential`` order
follows the pool line nearest at/after the game's anchor (wrapping);
``random`` picks a pool line per move, so popular replies win more
often. The engine is out of book at the first position with an empty
pool.

Indexing tokenizes movetext with regexes only (no move parsing) and is
cached by file stat. Per lookup, exactly one SAN token is parsed per
candidate tried, so book size never gates move latency.
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import chess

from ..config import BOOK_ORDER_RANDOM
from ..env_utils import env_int

log = logging.getLogger(__name__)

# Extension that marks a position book (one FEN per line); anything else
# is parsed as PGN. Matches fastchess format inference in fastchess.py.
_EPD_EXT = ".epd"

# Plies tokenized per line at index time; also the hard cap on book
# depth (the plies setting is clamped to it).
BOOK_INDEX_MAX_PLIES = env_int("SV_BOOK_INDEX_MAX_PLIES", 40)

# Book depth when the plies setting is unset.
_DEFAULT_BOOK_PLIES = 20

# Movetext cleanup so the cheap tokenizer below sees only SAN tokens: drop
# brace comments, $N NAGs, and the result token. Parenthesized variations
# are stripped separately (nesting-aware).
_PGN_COMMENT = re.compile(r"\{[^}]*\}")
_PGN_NAG = re.compile(r"\$\d+")
_PGN_RESULTS = {"*", "1-0", "0-1", "1/2-1/2"}
# Split a multi-game PGN on the blank-line gap before a new game's headers.
_PGN_GAME_SPLIT = re.compile(r"\n\s*\n(?=\[)")
# SAN decorations that don't identify the move: checks, mates, annotations.
_SAN_DECOR = re.compile(r"[+#!?]+$")
# Zero-style castling (after decoration strip) -> letter-O SAN.
_CASTLE_ZEROS = {"0-0": "O-O", "0-0-0": "O-O-O"}


@dataclass(slots=True, frozen=True)
class BookRef:
    """Frozen per-game reference to the PGN book HVE consults each engine
    turn. ``anchor`` is the sequential cursor at game start; modulo is
    applied per lookup, so the book file changing size mid-game is safe."""

    path: str
    plies: int | None
    order: str | None
    anchor: int


@dataclass(slots=True, frozen=True)
class _BookIndex:
    """Regex-only book index: no move parsing happens at build time."""

    is_epd: bool
    # EPD: raw FEN-ish lines (validity checked on selection).
    fens: tuple[str, ...] = ()
    # PGN: normalized SAN tokens per line, plus buckets keyed by the
    # first move's UCI so a lookup only prefix-compares lines that share
    # the first played move (SAN spelling variance can't split buckets).
    lines: tuple[tuple[str, ...], ...] = ()
    buckets: dict[str, tuple[int, ...]] = field(default_factory=dict)


# Book index cache keyed by file stat; invalidated when mtime/size changes.
_BookKey = tuple[str, int, int]
_index_cache: dict[_BookKey, _BookIndex] = {}


def is_epd_book(book_path: str) -> bool:
    """Format inference shared with fastchess: .epd = positions, else PGN."""
    return Path(book_path).suffix.lower() == _EPD_EXT


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


def _normalize_san(tok: str) -> str:
    """Canonical SAN spelling for matching: decorations and the promotion
    '=' dropped, zero-style castling mapped to letter-O."""
    tok = _SAN_DECOR.sub("", tok).replace("=", "")
    return _CASTLE_ZEROS.get(tok, tok)


def _san_tokens(movetext: str) -> tuple[str, ...]:
    """Normalized SAN tokens of one game's movetext, regex-only (no board):
    comments, NAGs, variations, results, and move numbers dropped. Capped
    at BOOK_INDEX_MAX_PLIES."""
    cleaned = _strip_variations(_PGN_NAG.sub(" ", _PGN_COMMENT.sub(" ", movetext)))
    toks: list[str] = []
    for tok in cleaned.split():
        if len(toks) >= BOOK_INDEX_MAX_PLIES:
            break
        if tok in _PGN_RESULTS:
            continue
        # Strip move numbers ("1." / "1..." / "12.e4") to bare SAN.
        tok = _normalize_san(tok.split(".")[-1])
        if not tok or tok[0].isdigit():
            continue
        toks.append(tok)
    return tuple(toks)


def _build_index(path: Path) -> _BookIndex:
    """One full-file pass: split into per-line entries and tokenize PGN
    movetext. Regex-only, so it stays fast even on large books; the result
    is cached by file stat in _load_index."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == _EPD_EXT:
        fens = tuple(ln for ln in (s.strip() for s in text.splitlines()) if ln)
        return _BookIndex(is_epd=True, fens=fens)
    lines = tuple(
        _san_tokens(_pgn_movetext(rec))
        for rec in _PGN_GAME_SPLIT.split(text)
        if rec.strip()
    )
    # Bucket by the first move's UCI, parsing each DISTINCT first token
    # once on startpos -- over-disambiguated spellings ("Ngf3") land in
    # the same bucket as the canonical "Nf3". Unparseable firsts (e.g. a
    # from-FEN game) get no bucket and can only be tried at ply 0.
    board = chess.Board()
    first_uci: dict[str, str | None] = {}
    buckets: dict[str, list[int]] = {}
    for i, toks in enumerate(lines):
        if not toks:
            continue
        tok = toks[0]
        if tok not in first_uci:
            try:
                first_uci[tok] = board.parse_san(tok).uci()
            except ValueError:
                first_uci[tok] = None
        uci = first_uci[tok]
        if uci is not None:
            buckets.setdefault(uci, []).append(i)
    return _BookIndex(
        is_epd=False,
        lines=lines,
        buckets={k: tuple(v) for k, v in buckets.items()},
    )


def _load_index(path: Path) -> _BookIndex:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _index_cache.get(key)
    if cached is None:
        # Evict superseded snapshots of the same file; without this the
        # cache would keep one full index per edit of the book.
        for stale in [k for k in _index_cache if k[0] == key[0]]:
            del _index_cache[stale]
        cached = _build_index(path)
        _index_cache[key] = cached
    return cached


def _pick_index(count: int, order: str | None, cursor: int) -> int:
    if order == BOOK_ORDER_RANDOM:
        return random.randrange(count)
    return cursor % count


def select_epd_seed(book_path: str, order: str | None, cursor: int) -> str | None:
    """Start FEN for a new game from an EPD book, or None (missing, empty,
    or non-EPD file). ``random`` picks a uniform line; anything else walks
    ``cursor % line_count``, probing forward (wrapping) past malformed
    lines so one bad entry doesn't yield "no opening"."""
    path = Path(book_path)
    if not path.is_file():
        return None
    idx = _load_index(path)
    if not idx.is_epd or not idx.fens:
        return None
    start = _pick_index(len(idx.fens), order, cursor)
    for off in range(len(idx.fens)):
        fen = _epd_to_fen(idx.fens[(start + off) % len(idx.fens)])
        if fen is not None:
            return fen
    return None


def _effective_plies(plies: int | None) -> int:
    return min(plies if plies is not None else _DEFAULT_BOOK_PLIES, BOOK_INDEX_MAX_PLIES)


def _line_matches(
    line: tuple[str, ...],
    k: int,
    history: tuple[str, ...],
    moves: list[chess.Move],
    boards: list[chess.Board],
) -> bool:
    """Does this book line cover the played prefix, with a move to spare?
    Tokens compare textually first; on mismatch the book token is parsed
    on that ply's board, so over-disambiguated SAN ("Ngf3" for Nf3) still
    matches instead of false-missing."""
    if len(line) <= k:
        return False
    for j in range(k):
        if line[j] == history[j]:
            continue
        try:
            if boards[j].parse_san(line[j]) != moves[j]:
                return False
        except ValueError:
            return False
    return True


def book_reply(
    book_path: str,
    played_uci: list[str],
    plies: int | None,
    order: str | None,
    anchor: int,
) -> str | None:
    """The engine's book move (UCI) for the position after ``played_uci``
    (startpos games only), or None when out of book.

    Pool = PGN lines whose tokens prefix-match the played moves. Order
    ``random`` tries pool lines in random order (line-frequency weighted);
    anything else tries them nearest at/after ``anchor`` (wrapping). Only
    the tried candidates' next tokens are SAN-parsed, and an unparseable
    or illegal token falls through to the next candidate."""
    path = Path(book_path)
    if not path.is_file():
        return None
    idx = _load_index(path)
    if idx.is_epd or not idx.lines:
        return None
    k = len(played_uci)
    if k >= _effective_plies(plies):
        log.debug("opening book: depth cap reached (%d plies) for %s", k, book_path)
        return None
    board = chess.Board()
    history: list[str] = []
    moves: list[chess.Move] = []
    boards: list[chess.Board] = []  # position before each played ply
    for uci in played_uci:
        try:
            move = chess.Move.from_uci(uci)
            history.append(_normalize_san(board.san(move)))
        except ValueError:
            return None
        moves.append(move)
        boards.append(board.copy(stack=False))
        board.push(move)
    prefix = tuple(history)
    candidates = idx.buckets.get(played_uci[0], ()) if played_uci else range(len(idx.lines))
    pool = [
        i for i in candidates
        if _line_matches(idx.lines[i], k, prefix, moves, boards)
    ]
    if not pool:
        log.debug(
            "opening book: no line matches [%s] in %s",
            " ".join(prefix), book_path,
        )
        return None
    n = len(idx.lines)
    if order == BOOK_ORDER_RANDOM:
        ordered = random.sample(pool, len(pool))
    else:
        ordered = sorted(pool, key=lambda i: (i - anchor) % n)
    for i in ordered:
        tok = idx.lines[i][k]
        try:
            move = board.parse_san(tok)
        except ValueError:
            continue
        log.debug(
            "opening book: playing %s (ply %d, line %d) from %s",
            move.uci(), k, i, book_path,
        )
        return move.uci()
    log.debug("opening book: no parseable reply at ply %d in %s", k, book_path)
    return None
