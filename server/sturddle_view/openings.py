"""Opening identification using the lichess-org/chess-openings dataset.

Loaded once at startup. Lookup is by current move sequence: we match the
longest registered prefix of the played moves and report its ECO + name.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import chess

from ._runtime import app_root
from .chess.pgn_walk import replay_line


DEFAULT_OPENINGS_DIR = app_root() / "web" / "vendor" / "chess-openings"


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

    def lookup(self, uci_moves: Iterable[str]) -> Optional[Opening]:
        """Return the most specific (deepest-ply) opening reached while
        replaying `uci_moves`. Returns None if no position along the line
        is registered.

        Caller is trusted to supply a legal move list; we don't validate
        per-ply legality. Malformed UCI yields a graceful early return;
        illegal-but-parseable moves will raise from chess.Board.push."""
        board = chess.Board()
        best: Optional[Opening] = None
        for uci in uci_moves:
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                return best
            board.push(move)
            hit = self._by_pos.get(board.epd())
            if hit is not None:
                best = hit
        return best

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

    # Process-wide cache: parsing the TSVs takes ~3s and the data is static.
    # The cache is keyed by directory path only and is NOT invalidated on
    # file changes — callers that mutate the openings directory at runtime
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
