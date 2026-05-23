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

from ._runtime import app_root


DEFAULT_OPENINGS_DIR = app_root() / "web" / "vendor" / "chess-openings"


@dataclass(slots=True, frozen=True)
class Opening:
    eco: str
    name: str


def _pgn_to_terminal_epd(pgn_text: str) -> Optional[str]:
    """Replay a PGN-style line and return the EPD of the terminal position
    (or None if the line is empty or unparseable).

    EPD = piece placement + side-to-move + castling + en-passant target
    (no halfmove/fullmove counters), so it identifies a position
    independent of how it was reached -- the basis for transposition-
    aware opening lookup."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return None
    board = game.board()
    moved = False
    for move in game.mainline_moves():
        board.push(move)
        moved = True
    return board.epd() if moved else None


class OpeningBook:
    """Position-keyed opening lookup.

    Each registered line is stored by the EPD of the position it reaches.
    Lookup replays the played moves and reports the opening whose
    position matches latest -- so transpositions to the same position
    resolve identically regardless of move order."""

    def __init__(self) -> None:
        # Maps position EPD -> Opening.
        self._by_pos: dict[str, Opening] = {}

    def __len__(self) -> int:
        return len(self._by_pos)

    def add(self, eco: str, name: str, pgn_text: str) -> None:
        epd = _pgn_to_terminal_epd(pgn_text)
        if epd is None:
            return
        # Earlier definitions win for the same position (rare).
        self._by_pos.setdefault(epd, Opening(eco=eco, name=name))

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
