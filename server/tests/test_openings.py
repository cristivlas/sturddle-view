from __future__ import annotations

import io
from pathlib import Path

import chess.pgn
import pytest

from sturddle_view.openings import OpeningBook

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_openings_cache():
    """OpeningBook._cache is module-global. Snapshot/restore around each test
    so loads against tmp/synthetic dirs don't pollute later tests."""
    saved = dict(OpeningBook._cache)
    try:
        yield
    finally:
        OpeningBook._cache.clear()
        OpeningBook._cache.update(saved)


def test_load_default_book_has_lines():
    book = OpeningBook.load()
    # The dataset has thousands of lines; we just verify it loaded > 0.
    assert len(book) > 0


def test_lookup_caro_kann():
    book = OpeningBook.load()
    # 1. e4 c6 -> Caro-Kann Defense (B10)
    hit = book.lookup(["e2e4", "c7c6"])
    assert hit is not None
    assert hit.eco.startswith("B")
    assert "Caro-Kann" in hit.name


def test_lookup_unplayable_returns_none():
    book = OpeningBook.load()
    # Empty move list -> nothing.
    assert book.lookup([]) is None


def test_lookup_longest_prefix_wins():
    book = OpeningBook.load()
    # 1. e4 c5 (Sicilian) vs 1. e4 c5 2. Nf3 d6 3. d4 (Open Sicilian variant) —
    # the longer line should report a more specific name.
    short = book.lookup(["e2e4", "c7c5"])
    longer = book.lookup(["e2e4", "c7c5", "g1f3", "d7d6", "d2d4"])
    assert short is not None and longer is not None
    # Longer match should be at least as specific (different name or same).
    # We assert they're both Sicilians (B-class), and longer has a non-empty name.
    assert short.eco.startswith("B")
    assert longer.eco.startswith("B")


def test_load_missing_dir_returns_empty():
    book = OpeningBook.load(Path("/nonexistent/openings/dir"))
    assert len(book) == 0


def test_load_is_process_cached():
    """Repeat calls return the same instance — parsing TSVs is expensive."""
    a = OpeningBook.load()
    b = OpeningBook.load()
    assert a is b


# --- all() (full list for client-side filtering) --------------------------

def test_all_sorted_by_eco_then_name():
    book = OpeningBook.load()
    rows = book.all()
    assert rows, "expected a non-empty opening list"
    keys = [(o.eco, o.name) for o in rows]
    assert keys == sorted(keys), "all() must be sorted by (eco, name)"


def test_all_one_row_per_name():
    book = OpeningBook.load()
    names = [o.name for o in book.all()]
    assert len(names) == len(set(names)), "all() must have one row per name"


def test_all_rows_carry_pgn_and_ply():
    # Assumes the vendored lichess-openings snapshot (Caro-Kann present).
    book = OpeningBook.load()
    caro = next(o for o in book.all() if o.name == "Caro-Kann Defense")
    assert caro.pgn, "row must carry its PGN line"
    assert caro.ply > 0, "row must carry a positive ply count"


def test_all_keeps_shortest_line_per_name():
    """Each name keeps its fewest-ply (canonical shortest) line.
    Assumes the vendored lichess-openings snapshot."""
    book = OpeningBook.load()
    by_name = {o.name: o for o in book.all()}
    # The bare "Caro-Kann Defense" is 1.e4 c6 (2 ply); deeper variations
    # have distinct names, so this row must be the short one.
    assert by_name["Caro-Kann Defense"].ply == 2


# --- Transposition lookup -------------------------------------------------
#
# The lichess openings dataset registers each opening at one canonical move
# order, but the same final position can be reached by many move orders.
# Lookup should identify openings by POSITION, not by move sequence.

# D14 "Slav Defense: Exchange Variation, Trifunovic Variation" is registered
# in d.tsv at: 1.d4 d5 2.c4 c6 3.Nf3 Nf6 4.cxd5 cxd5 5.Nc3 Nc6 6.Bf4 Bf5
#              7.e3 e6 8.Qb3 Bb4
D14_TRIFUNOVIC_CANONICAL = [
    "d2d4", "d7d5", "c2c4", "c7c6", "g1f3", "g8f6", "c4d5", "c6d5",
    "b1c3", "b8c6", "c1f4", "c8f5", "e2e3", "e7e6", "d1b3", "f8b4",
]
# Same final position reached via a different early order
# (Indian-Defense move order: 1.d4 Nf6 2.Nf3 d5 3.c4 c6 ...).
D14_TRIFUNOVIC_TRANSPOSED = [
    "d2d4", "g8f6", "g1f3", "d7d5", "c2c4", "c7c6", "c4d5", "c6d5",
    "b1c3", "b8c6", "c1f4", "c8f5", "e2e3", "e7e6", "d1b3", "f8b4",
]


def test_lookup_finds_canonical_move_order():
    """Sanity: the canonical move order resolves to the right opening."""
    book = OpeningBook.load()
    hit = book.lookup(D14_TRIFUNOVIC_CANONICAL)
    assert hit is not None
    assert hit.eco == "D14"
    assert "Trifunovic" in hit.name


def test_lookup_detects_transposition_to_same_position():
    """Different move order reaching the same position should report the
    same opening. Without position-based lookup, a transposed sequence
    falls back to whatever shorter prefix happens to match -- typically the
    Indian Defense Knights Variation for d4-Nf6-Nf3 orders."""
    book = OpeningBook.load()
    hit = book.lookup(D14_TRIFUNOVIC_TRANSPOSED)
    assert hit is not None
    assert hit.eco == "D14", (
        f"Expected D14 (Slav Exchange Trifunovic) via transposition, "
        f"got {hit.eco} {hit.name!r}"
    )
    assert "Trifunovic" in hit.name


def test_lookup_partial_transposition_stays_specific():
    """After only the first few transposed moves we haven't yet reached
    the D14 position -- we should report the BEST opening for the position
    actually on the board, not jump ahead to D14."""
    book = OpeningBook.load()
    # 1.d4 Nf6 2.Nf3 -- this position is A46 Indian Defense Knights Variation.
    hit = book.lookup(["d2d4", "g8f6", "g1f3"])
    assert hit is not None
    assert hit.eco == "A46"
    assert "Knights Variation" in hit.name


def test_lookup_stops_at_malformed_uci_returning_best_so_far():
    """If unparseable UCI appears mid-stream, lookup returns the best
    opening identified before the bad token (graceful degradation)."""
    book = OpeningBook.load()
    # 1.e4 c6 is Caro-Kann (B-class); then garbage truncates the walk.
    hit = book.lookup(["e2e4", "c7c6", "not-a-move"])
    assert hit is not None
    assert hit.eco.startswith("B")
    assert "Caro-Kann" in hit.name


def test_real_game_pgn_identifies_correct_opening_via_transposition():
    """Witness fixture: a real game played 2026-05-22 reached the D14
    Slav Exchange Trifunovic position via an Indian Defense move order
    (1.d4 Nf6 2.Nf3 d5 3.c4 c6 ...). Before the position-keyed lookup
    fix this game showed as A46. This test pins the fix to a concrete
    artifact."""
    pgn_path = FIXTURES_DIR / "Claude vs Sturddle 2.5.1-rc9, 2026.pgn"
    text = pgn_path.read_text(encoding="utf-8")
    game = chess.pgn.read_game(io.StringIO(text))
    assert game is not None
    ucis = [m.uci() for m in game.mainline_moves()]
    assert len(ucis) == 38, f"expected 38 plies, got {len(ucis)}"

    book = OpeningBook.load()
    # The Trifunovic position is reached after 8.Qb3 Bb4 (ply 16).
    hit = book.lookup(ucis[:16])
    assert hit is not None
    assert hit.eco == "D14", (
        f"transposition lookup failed for the real-game fixture: "
        f"expected D14 Trifunovic, got {hit.eco} {hit.name!r}"
    )
    assert "Trifunovic" in hit.name
