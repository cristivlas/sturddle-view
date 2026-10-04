"""Opening identification using the lichess-org/chess-openings dataset.

Loaded once at startup. Lookup is by current move sequence: we match the
longest registered prefix of the played moves and report its ECO + name.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, NamedTuple, Optional, Sequence

import chess

from ._runtime import app_root
from .chess.pgn_walk import replay_line
from .env_utils import env_int


DEFAULT_OPENINGS_DIR = app_root() / "web" / "vendor" / "chess-openings"

# A word for name matching; an inner apostrophe (straight or curly) joins
# ("King's"). Other punctuation separates words and is dropped.
_NAME_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*")
_APOSTROPHE_RE = re.compile(r"['\u2019]")
# Sentence punctuation in a gap between words becomes its own token, so a
# match spans one only where the name has one ("St. George", not "the
# Sicilian. Defense").
_SENTENCE_GAP_RE = re.compile(r"[.!?;]")
_GAP_TOKEN = "."
# Trie key marking "a name ends here"; never a word (words are non-empty).
_TRIE_END = ""
# A name's parts ("Family: Variation, Sub-variation"); a short form drops
# leading parts at one of these.
_NAME_PART_SEP_RE = re.compile(r"[:,]")
# What a possessive leaves on a word once its apostrophe is dropped
# ("gambit's" -> "gambits").
_POSSESSIVE_SUFFIX = "s"
# Fewest words in a short form: a one-word tail ("Closed") is ordinary prose.
_DEFAULT_SHORT_FORM_MIN_WORDS = 2
SHORT_FORM_MIN_WORDS = env_int(
    "SV_AI_OPENING_SHORT_FORM_MIN_WORDS", _DEFAULT_SHORT_FORM_MIN_WORDS
)


class _NameToken(NamedTuple):
    """One word of a name or of prose, apostrophe-free: `exact` as written,
    `folded` casefolded; [start, end) is its span in the source text."""
    folded: str
    exact: str
    start: int
    end: int


def _name_tokens(text: str) -> list[_NameToken]:
    """The words of `text`, with a _GAP_TOKEN for each sentence-punctuation
    gap between two of them."""
    tokens: list[_NameToken] = []
    prev_end: int | None = None
    for m in _NAME_WORD_RE.finditer(text):
        if prev_end is not None and _SENTENCE_GAP_RE.search(text, prev_end, m.start()):
            tokens.append(_NameToken(_GAP_TOKEN, _GAP_TOKEN, prev_end, m.start()))
        word = _APOSTROPHE_RE.sub("", m.group())
        tokens.append(_NameToken(word.casefold(), word, m.start(), m.end()))
        prev_end = m.end()
    return tokens


@dataclass(slots=True, frozen=True)
class Opening:
    eco: str
    name: str
    pgn: str = ""  # canonical move sequence (SAN), as shipped in the dataset
    ply: int = 0  # number of plies in `pgn`
    moves: tuple[str, ...] = ()  # UCI moves of `pgn`, for proximity ranking

    def as_dict(self) -> dict:
        """Public wire shape (eco, name, pgn, ply); `moves` is internal."""
        return {"eco": self.eco, "name": self.name, "pgn": self.pgn, "ply": self.ply}


def _short_forms(name: str) -> Iterator[tuple[str, ...]]:
    """The exact-case word tuples `name` goes by with leading parts dropped
    ("Bellon Gambit" for "English Opening: King's English Variation, Bellon
    Gambit"). Each has at least SHORT_FORM_MIN_WORDS words and opens with a
    capital: a tail like "with d5" is ordinary prose."""
    for sep in _NAME_PART_SEP_RE.finditer(name):
        words = tuple(t.exact for t in _name_tokens(name[sep.end():]))
        if (
            sum(w != _GAP_TOKEN for w in words) >= SHORT_FORM_MIN_WORDS
            and words[0][0].isupper()
        ):
            yield words


def _longest_at(
    words: Sequence[str], i: int, forms: Mapping[tuple[str, ...], object], limit: int,
) -> int:
    """Word count of the longest of `forms` written at words[i:]; 0 if none.
    `limit` is the longest form's word count."""
    for n in range(min(limit, len(words) - i), 0, -1):
        if tuple(words[i:i + n]) in forms:
            return n
    return 0


def _lead_word(word: str, names: Sequence[frozenset[str]]) -> str | None:
    """`word` (casefolded) as a word of one of `names`, or its possessive's
    base ("gambits" -> "gambit"); None if neither is."""
    for candidate in (word, word.removesuffix(_POSSESSIVE_SUFFIX)):
        if any(candidate in name for name in names):
            return candidate
    return None


def _lead_run_start(
    tokens: Sequence[_NameToken],
    i: int,
    form: tuple[str, ...],
    own: frozenset[str],
    book: OpeningBook,
) -> int:
    """Where another opening's name starts when it leads into the short form
    `form` at tokens[i]: the capitalized words right before it, all words of
    one other book name going by `form`, one of them not in `own` (the
    opening's own name words). "French" in "French Exchange Variation" (the
    French Defense's); not "Black" in "Black's Advance Variation" (no such
    opening). `i` when there is none."""
    others = [name for name in book.names_by_short_form(form) if name != own]
    j = i
    foreign = False
    while j > 0 and others:
        word = tokens[j - 1].exact
        if not word[0].isupper():
            break
        lead = _lead_word(word.casefold(), others)
        if lead is None:
            break
        others = [name for name in others if lead in name]
        j -= 1
        foreign = foreign or lead not in own
    return j if foreign else i


def find_opening_names(
    text: str, openings: Iterable[Opening], book: OpeningBook | None = None,
) -> list[tuple[str, Opening | None]]:
    """Opening names in `text`, as (surface, opening) in text order;
    `surface` is the exact span of `text`, `opening` the one of `openings`
    it names. Words compare with punctuation dropped, never across sentence
    punctuation the name lacks. A full name matches in any case; a short
    form (see `_short_forms`) only as the book capitalizes it. Longest match
    wins at each word; no overlaps.

    `opening` is None for a name that must not be linked: a short form that
    fits two of `openings`, or (with `book`) one that names another opening
    -- its full name ("French Defense Exchange Variation") or its words
    leading into a short form it shares ("French Exchange Variation"). Such
    a name is taken whole, so the short form inside it is not linked."""
    full: dict[tuple[str, ...], Opening] = {}
    # None marks a short form that fits more than one opening.
    short: dict[tuple[str, ...], Opening | None] = {}
    own_words: dict[str, frozenset[str]] = {}
    for opening in openings:
        name_words = tuple(t.folded for t in _name_tokens(opening.name))
        full.setdefault(name_words, opening)
        own_words[opening.name] = frozenset(name_words)
        for words in _short_forms(opening.name):
            owner = short.setdefault(words, opening)
            if owner is not None and owner.name != opening.name:
                short[words] = None
    full_limit = max(map(len, full), default=0)
    short_limit = max(map(len, short), default=0)
    tokens = _name_tokens(text)
    folded = [t.folded for t in tokens]
    exact = [t.exact for t in tokens]
    # (first token, end token exclusive, opening) per name found.
    spans: list[tuple[int, int, Opening | None]] = []
    i = 0
    while i < len(tokens):
        n_full = _longest_at(folded, i, full, full_limit)
        if book is not None:
            n_full = max(n_full, book.name_length_at(folded, i))
        n_short = _longest_at(exact, i, short, short_limit)
        n = max(n_full, n_short)
        if n == 0:
            i += 1
            continue
        first = i
        # A full name outranks a short form of its length; one that is not
        # in `openings` names no opening of ours.
        if n_full >= n_short:
            hit = full.get(tuple(folded[i:i + n]))
        else:
            form = tuple(exact[i:i + n])
            hit = short[form]
            if hit is not None and book is not None:
                first = _lead_run_start(tokens, i, form, own_words[hit.name], book)
        if first < i:
            # Another name leads into the short form: the phrase, with any
            # name already found in it, is one name that is not ours.
            hit = None
            while spans and spans[-1][1] > first:
                first = min(first, spans.pop()[0])
        spans.append((first, i + n, hit))
        i += n
    return [
        (text[tokens[a].start:tokens[b - 1].end], hit) for a, b, hit in spans
    ]


class OpeningBook:
    """Position-keyed opening lookup.

    Each registered line is stored by the EPD of the position it reaches.
    Lookup replays the played moves and reports the opening whose
    position matches latest -- so transpositions to the same position
    resolve identically regardless of move order."""

    def __init__(self) -> None:
        # Maps position EPD -> Opening.
        self._by_pos: dict[str, Opening] = {}
        # Shortest registered line per name (for name/ECO search). Multiple
        # transposition rows share a name; we keep the fewest-ply entry.
        self._by_name: dict[str, Opening] = {}
        # Built over _by_name on first use: the word trie of the full names
        # (name_length_at) and their word sets by short form
        # (names_by_short_form).
        self._name_trie: dict | None = None
        self._short_form_names: dict[tuple[str, ...], list[frozenset[str]]] | None = None

    def __len__(self) -> int:
        return len(self._by_pos)

    def add(self, eco: str, name: str, pgn_text: str) -> None:
        epd, moves = replay_line(pgn_text)
        if epd is None:
            return
        opening = Opening(
            eco=eco, name=name, pgn=pgn_text, ply=len(moves), moves=moves,
        )
        # Earlier definitions win for the same position (rare).
        self._by_pos.setdefault(epd, opening)
        existing = self._by_name.get(name)
        if existing is None or opening.ply < existing.ply:
            self._by_name[name] = opening
            self._name_trie = None
            self._short_form_names = None

    def lookup(self, uci_moves: Iterable[str]) -> Optional[Opening]:
        """Return the most specific (deepest-ply) opening reached while
        replaying `uci_moves`. Returns None if no position along the line
        is registered.

        Caller is trusted to supply a legal move list; we don't validate
        per-ply legality. Malformed UCI yields a graceful early return;
        illegal-but-parseable moves will raise from chess.Board.push."""
        best: Optional[Opening] = None
        for hit in self._walk(chess.Board(), uci_moves, check_legal=False):
            if hit is not None:
                best = hit
        return best

    def line_openings(self, fen: str, uci_moves: Iterable[str]) -> list[Optional[Opening]]:
        """The opening registered at each position `uci_moves` reaches from
        `fen` (None where there is none), up to the first malformed or
        illegal move: a client's line may belong to another position.
        Raises ValueError for a bad `fen`."""
        return list(self._walk(chess.Board(fen), uci_moves, check_legal=True))

    def _walk(
        self, board: chess.Board, uci_moves: Iterable[str], check_legal: bool,
    ) -> Iterator[Optional[Opening]]:
        """The opening registered (or None) at each position `uci_moves`
        reaches on `board`, until a malformed move or, when `check_legal`,
        an illegal one."""
        for uci in uci_moves:
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                return
            if check_legal and not board.is_legal(move):
                return
            board.push(move)
            yield self._by_pos.get(board.epd())

    def all(self) -> list[Opening]:
        """Every opening (one row per name, shortest line), sorted ECO then
        name. The client loads this once and filters locally."""
        return sorted(self._by_name.values(), key=lambda o: (o.eco, o.name))

    @staticmethod
    def family_of(name: str) -> str:
        """The opening family: the name up to the first ':' -- the lichess
        dataset's 'Family: Variation' convention (e.g. 'Sicilian Defense:
        Najdorf Variation' -> 'Sicilian Defense'). A name without a colon is
        its own family. Whitespace-stripped."""
        return name.split(":", 1)[0].strip()

    def by_family(self, family: str, *, limit: int | None = None) -> list[Opening]:
        """Openings in `family` (one row per name, shortest line), in the
        same (eco, name) order as `all()`. `family` is matched
        case-insensitively and reduced via `family_of` first, so a full
        variation name selects its whole family. `limit` caps the result
        (None = uncapped)."""
        target = self.family_of(family).casefold()
        rows = [o for o in self.all() if self.family_of(o.name).casefold() == target]
        return rows[:limit] if limit is not None else rows

    def nearest(
        self,
        line_uci: Iterable[str],
        *,
        family: str | None = None,
        limit: int | None = None,
    ) -> list[Opening]:
        """Openings closest to `line_uci` by shared move-prefix, longest
        shared prefix first -- the move-tree neighborhood of the current
        line, i.e. the named variations that branch at or just before it.
        Ties break toward the shorter (more fundamental) line, then
        (eco, name). `family` (see `family_of`) restricts the pool; None
        ranks the whole book.

        `line_uci` is the played moves in UCI. Ranking by proximity to it
        surfaces the relevant relatives (the d6-Sicilians for 1.e4 c5
        2.Nf3 d6) instead of an alphabetical slice of a name-family. An
        empty line falls back to fundamental-first (shortest) order."""
        line = tuple(line_uci)
        pool = self.by_family(family) if family is not None else self.all()

        def shared_prefix(o: Opening) -> int:
            n = 0
            for a, b in zip(line, o.moves):
                if a != b:
                    break
                n += 1
            return n

        ranked = sorted(
            pool, key=lambda o: (-shared_prefix(o), o.ply, o.eco, o.name)
        )
        return ranked[:limit] if limit is not None else ranked

    def continuations(
        self, line_uci: Iterable[str], *, limit: int | None = None,
    ) -> list[Opening]:
        """Openings whose move list strictly extends `line_uci` -- named
        theory continuing the position. Shortest (most fundamental) line
        first, ties toward (eco, name); unlike `nearest`, lines that
        branch before the position are excluded."""
        line = tuple(line_uci)
        n = len(line)
        pool = [
            o for o in self.all() if len(o.moves) > n and o.moves[:n] == line
        ]
        pool.sort(key=lambda o: (o.ply, o.eco, o.name))
        return pool[:limit] if limit is not None else pool

    def _trie(self) -> dict:
        if self._name_trie is None:
            root: dict = {}
            for name in self._by_name:
                node = root
                for token in _name_tokens(name):
                    node = node.setdefault(token.folded, {})
                node[_TRIE_END] = True
            self._name_trie = root
        return self._name_trie

    def name_length_at(self, words: Sequence[str], i: int) -> int:
        """Word count of the longest book name written at words[i:]
        (casefolded words, see `_name_tokens`); 0 if none."""
        node = self._trie()
        best = 0
        for j in range(i, len(words)):
            node = node.get(words[j])
            if node is None:
                break
            if _TRIE_END in node:
                best = j - i + 1
        return best

    def names_by_short_form(self, form: tuple[str, ...]) -> list[frozenset[str]]:
        """Casefolded word sets of the book names that go by the short form
        `form` (see `_short_forms`)."""
        if self._short_form_names is None:
            index: dict[tuple[str, ...], list[frozenset[str]]] = {}
            for name in self._by_name:
                words = frozenset(t.folded for t in _name_tokens(name))
                for short_form in _short_forms(name):
                    index.setdefault(short_form, []).append(words)
            self._short_form_names = index
        return self._short_form_names.get(form, [])

    # Process-wide cache: parsing the TSVs takes ~3s and the data is static.
    # The cache is keyed by directory path only and is NOT invalidated on
    # file changes -- callers that mutate the openings directory at runtime
    # must clear `_cache` themselves. (Production data ships read-only with
    # the app; tests use the same default dir.)
    _cache: dict[Path, "OpeningBook"] = {}

    @classmethod
    def load(cls, dir_path: Path | None = None) -> "OpeningBook":
        d = (dir_path or DEFAULT_OPENINGS_DIR).resolve()
        cached = cls._cache.get(d)
        if cached is not None:
            return cached
        book = cls()
        if not d.is_dir():
            cls._cache[d] = book
            return book
        for tsv in sorted(d.glob("*.tsv")):
            with tsv.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter="\t")
                for row in reader:
                    eco = (row.get("eco") or "").strip()
                    name = (row.get("name") or "").strip()
                    pgn = (row.get("pgn") or "").strip()
                    if not eco or not name or not pgn:
                        continue
                    book.add(eco, name, pgn)
        cls._cache[d] = book
        return book
