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
import chess.pgn
import io


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPENINGS_DIR = REPO_ROOT / "web" / "vendor" / "chess-openings"


@dataclass(slots=True, frozen=True)
class Opening:
    eco: str
    name: str


def _pgn_to_uci_sequence(pgn_text: str) -> tuple[str, ...]:
    """Convert PGN-style move text (e.g. '1. e4 e5 2. Nf3') to a tuple of UCI moves."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return ()
    board = game.board()
    moves: list[str] = []
    for move in game.mainline_moves():
        moves.append(move.uci())
        board.push(move)
    return tuple(moves)


class OpeningBook:
    """Trie-like lookup keyed by UCI move sequence."""

    def __init__(self) -> None:
        # Maps move-sequence tuple -> Opening.
        self._by_seq: dict[tuple[str, ...], Opening] = {}

    def __len__(self) -> int:
        return len(self._by_seq)

    def add(self, eco: str, name: str, pgn_text: str) -> None:
        seq = _pgn_to_uci_sequence(pgn_text)
        if not seq:
            return
        # Earlier definitions win for the same sequence (rare).
        self._by_seq.setdefault(seq, Opening(eco=eco, name=name))

    def lookup(self, uci_moves: Iterable[str]) -> Optional[Opening]:
        """Return the longest registered opening that prefixes `uci_moves`."""
        moves = tuple(uci_moves)
        # Walk backwards from the longest possible prefix to the shortest.
        for n in range(min(len(moves), 30), 0, -1):
            hit = self._by_seq.get(moves[:n])
            if hit is not None:
                return hit
        return None

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
