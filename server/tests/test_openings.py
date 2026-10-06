from __future__ import annotations

import io
from pathlib import Path

import chess
import chess.pgn
import pytest

from sturddle_view.openings import OpeningBook, find_opening_names

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
    # 1. e4 c5 (Sicilian) vs 1. e4 c5 2. Nf3 d6 3. d4 (Open Sicilian variant) --
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
    """Repeat calls return the same instance -- parsing TSVs is expensive."""
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


# --- line_openings ----------------------------------------------------------

AFTER_CARO_KANN_FEN = "rnbqkbnr/pp1ppppp/2p5/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


def _names(hits):
    return [hit and hit.name for hit in hits]


def test_line_openings_reports_each_position():
    book = OpeningBook.load()
    hits = book.line_openings(chess.STARTING_FEN, ["e2e4", "c7c6", "h2h4"])
    assert _names(hits) == ["King's Pawn Game", "Caro-Kann Defense", None]


def test_line_openings_starts_from_fen():
    book = OpeningBook.load()
    hits = book.line_openings(AFTER_CARO_KANN_FEN, ["d2d4", "d7d5", "e4e5"])
    assert _names(hits)[-1] == "Caro-Kann Defense: Advance Variation"


@pytest.mark.parametrize("bad", ["not-a-move", "e2e4"])
def test_line_openings_stops_at_malformed_or_illegal_move(bad):
    """A line belonging to another position stops where it stops fitting."""
    book = OpeningBook.load()
    hits = book.line_openings(chess.STARTING_FEN, ["e2e4", bad, "c7c6"])
    assert _names(hits) == ["King's Pawn Game"]


def test_line_openings_bad_fen_raises():
    with pytest.raises(ValueError):
        OpeningBook.load().line_openings("not a fen", ["e2e4"])


# --- family_of / by_family ------------------------------------------------


def test_family_of_splits_at_colon():
    assert OpeningBook.family_of("Sicilian Defense: Najdorf Variation") == "Sicilian Defense"
    # Sub-variation after the colon keeps the family before it.
    assert (
        OpeningBook.family_of("Slav Defense: Exchange Variation, Trifunovic Variation")
        == "Slav Defense"
    )


def test_family_of_no_colon_is_own_family():
    assert OpeningBook.family_of("Caro-Kann Defense") == "Caro-Kann Defense"


def test_family_of_strips_whitespace():
    assert OpeningBook.family_of("  Sicilian Defense :  Najdorf  ") == "Sicilian Defense"


def test_by_family_groups_variations():
    book = OpeningBook.load()
    rows = book.by_family("Caro-Kann Defense")
    assert rows, "expected Caro-Kann variations in the vendored dataset"
    # Every row shares the family, and the bare line is among them.
    assert all(OpeningBook.family_of(o.name) == "Caro-Kann Defense" for o in rows)
    assert any(o.name == "Caro-Kann Defense" for o in rows)
    # The family has named sub-variations, not just the bare line.
    assert any(":" in o.name for o in rows)


def test_by_family_accepts_full_variation_name():
    """A full 'Family: Variation' name selects the whole family, same as
    passing the family alone."""
    book = OpeningBook.load()
    by_family = {o.name for o in book.by_family("Caro-Kann Defense")}
    by_variation = {
        o.name for o in book.by_family("Caro-Kann Defense: Advance Variation")
    }
    assert by_family == by_variation


def test_by_family_is_case_insensitive():
    book = OpeningBook.load()
    lower = {o.name for o in book.by_family("caro-kann defense")}
    canonical = {o.name for o in book.by_family("Caro-Kann Defense")}
    assert lower == canonical and lower


def test_by_family_limit_caps_results():
    book = OpeningBook.load()
    # Sicilian is large; a small limit must cap the row count.
    capped = book.by_family("Sicilian Defense", limit=3)
    assert len(capped) == 3
    full = book.by_family("Sicilian Defense")
    assert len(full) > 3, "expected the Sicilian family to exceed the cap"


def test_by_family_preserves_all_order():
    """by_family is a filter over all(), so it keeps the (eco, name) order."""
    book = OpeningBook.load()
    rows = book.by_family("Sicilian Defense")
    keys = [(o.eco, o.name) for o in rows]
    assert keys == sorted(keys)


def test_by_family_unknown_returns_empty():
    book = OpeningBook.load()
    assert book.by_family("Not A Real Opening Family") == []


# --- find_opening_names (a turn's openings named in prose) ----------------

_SCHOFMAN = "Sicilian Defense: Grand Prix Attack, Schofman Variation"
_BELLON_ENGLISH = "English Opening: King's English Variation, Bellon Gambit"
_BELLON_DUTCH = "Dutch Defense: Bellon Gambit"
_CLOSED_SICILIAN = "Sicilian Defense: Closed"
_BENKO_MAIN_LINE = "Benko Gambit Declined: Main Line"
_CARO_KANN_EXCHANGE = "Caro-Kann Defense: Exchange Variation"
_CARO_KANN_ADVANCE = "Caro-Kann Defense: Advance Variation"
_TAIMANOV_ENGLISH_ATTACK = (
    "Sicilian Defense: Taimanov Variation, Bastrikov Variation, English Attack"
)
_POLISH_WITH_D5 = "Polish Opening, with d5"
_CURLY_APOSTROPHE = chr(0x2019)


def _found(text: str, *names: str) -> list[tuple[str, str | None]]:
    """(surface, opening name) for `text` scanned against the named openings;
    the name is None for a span found but not to be linked."""
    book = OpeningBook.load()
    by_name = {o.name: o for o in book.all()}
    hits = find_opening_names(text, [by_name[n] for n in names], book)
    return [(surface, o.name if o else None) for surface, o in hits]


def test_find_full_name_ignores_punctuation():
    text = "The Sicilian Defense Grand Prix Attack Schofman Variation features f5."
    assert _found(text, _SCHOFMAN) == [
        ("Sicilian Defense Grand Prix Attack Schofman Variation", _SCHOFMAN),
    ]


def test_find_full_name_ignores_case_and_apostrophe_style():
    surface = f"king{_CURLY_APOSTROPHE}s indian defense"
    assert _found(f"unlike the {surface}", "King's Indian Defense") == [
        (surface, "King's Indian Defense"),
    ]


def test_find_matches_sentence_punctuation_the_name_has():
    assert _found("Black tried the St. George Defense.", "St. George Defense") == [
        ("St. George Defense", "St. George Defense"),
    ]


def test_find_never_spans_a_sentence_break():
    text = "He knew the Sicilian. Defense mattered more."
    assert _found(text, "Sicilian Defense") == []


def test_find_another_openings_full_name_is_not_linked():
    assert _found("The Italian Game is solid.", "Caro-Kann Defense") == [
        ("Italian Game", None),
    ]


def test_find_reports_every_name_in_text_order():
    text = "The Italian Game, then the Caro-Kann Defense."
    assert _found(text, "Caro-Kann Defense", "Italian Game") == [
        ("Italian Game", "Italian Game"),
        ("Caro-Kann Defense", "Caro-Kann Defense"),
    ]


def test_find_short_form_drops_leading_parts():
    assert _found("Unlike the Bellon Gambit, this is calm.", _BELLON_ENGLISH) == [
        ("Bellon Gambit", _BELLON_ENGLISH),
    ]
    text = "The Grand Prix Attack Schofman Variation branches early."
    assert _found(text, _SCHOFMAN) == [
        ("Grand Prix Attack Schofman Variation", _SCHOFMAN),
    ]


def test_find_takes_the_longest_form():
    text = "The King's English Variation, Bellon Gambit is sharp."
    assert _found(text, _BELLON_ENGLISH) == [
        ("King's English Variation, Bellon Gambit", _BELLON_ENGLISH),
    ]


def test_find_short_form_fitting_two_openings_links_neither():
    text = "Unlike the Bellon Gambit, this is calm."
    assert _found(text, _BELLON_ENGLISH, _BELLON_DUTCH) == [("Bellon Gambit", None)]


def test_find_short_form_needs_two_words():
    assert _found("The Closed center favors White.", _CLOSED_SICILIAN) == []


def test_find_short_form_must_open_with_a_capital():
    # "with d5" is the tail of the name, and ordinary chess prose.
    assert _found("Black answers with d5 at once.", _POLISH_WITH_D5) == []
    assert _found("The Polish Opening with d5 is rare.", _POLISH_WITH_D5) == [
        ("Polish Opening with d5", _POLISH_WITH_D5),
    ]


def test_find_short_form_inside_another_openings_name_is_not_linked():
    text = "Unlike the French Defense Exchange Variation, this is calm."
    assert _found(text, _CARO_KANN_EXCHANGE) == [
        ("French Defense Exchange Variation", None),
    ]
    assert _found("The Exchange Variation is calm.", _CARO_KANN_EXCHANGE) == [
        ("Exchange Variation", _CARO_KANN_EXCHANGE),
    ]


def test_find_short_form_after_another_names_words_is_not_linked():
    # A reworded name of another opening: no full-name match to go by.
    text = "Unlike the French Exchange Variation, this is calm."
    assert _found(text, _CARO_KANN_EXCHANGE) == [
        ("French Exchange Variation", None),
    ]
    # Another opening's full name, then the short form: one name, not ours.
    text = "The Queen's Gambit Exchange Variation is calm."
    assert _found(text, _CARO_KANN_EXCHANGE) == [
        ("Queen's Gambit Exchange Variation", None),
    ]


def test_find_short_form_after_another_names_variation_word_is_not_linked():
    # The lead is a variation of another family, not a family word.
    text = "Unlike the Winawer Advance Variation, this is calm."
    assert _found(text, _CARO_KANN_ADVANCE) == [
        ("Winawer Advance Variation", None),
    ]
    text = "Unlike the Najdorf English Attack, this is calm."
    assert _found(text, _TAIMANOV_ENGLISH_ATTACK) == [
        ("Najdorf English Attack", None),
    ]


def test_find_short_form_after_a_possessive_foreign_name_is_not_linked():
    text = "The Queen's Gambit's Exchange Variation is calm."
    assert _found(text, _CARO_KANN_EXCHANGE) == [
        ("Queen's Gambit's Exchange Variation", None),
    ]


def test_find_short_form_after_its_own_names_words_is_linked():
    text = "The Caro-Kann Exchange Variation is calm."
    assert _found(text, _CARO_KANN_EXCHANGE) == [
        ("Exchange Variation", _CARO_KANN_EXCHANGE),
    ]
    text = "The Caro-Kann's Exchange Variation is calm."
    assert _found(text, _CARO_KANN_EXCHANGE) == [
        ("Exchange Variation", _CARO_KANN_EXCHANGE),
    ]
    text = "Unlike the Taimanov English Attack, this is calm."
    assert _found(text, _TAIMANOV_ENGLISH_ATTACK) == [
        ("English Attack", _TAIMANOV_ENGLISH_ATTACK),
    ]


def test_find_short_form_after_a_word_no_other_such_opening_has_is_linked():
    # A lead blocks only as words of another opening going by the same short
    # form: no Advance Variation opening has "The" or "Black" in its name.
    assert _found("The Advance Variation is sharp.", _CARO_KANN_ADVANCE) == [
        ("Advance Variation", _CARO_KANN_ADVANCE),
    ]
    text = "Black's Advance Variation is sharp."
    assert _found(text, _CARO_KANN_ADVANCE) == [
        ("Advance Variation", _CARO_KANN_ADVANCE),
    ]


def test_find_short_form_after_a_word_another_such_opening_has_is_not_linked():
    # "King's Indian Defense: Exchange Variation" goes by it too.
    text = "Unlike the King's Exchange Variation, this is calm."
    assert _found(text, _CARO_KANN_EXCHANGE) == [
        ("King's Exchange Variation", None),
    ]


def test_find_foreign_phrase_takes_in_a_name_found_before_it():
    # "Queen's Gambit" alone would link, but here it leads into its own
    # family's Exchange Variation: the whole phrase is one plain name.
    text = "The Queen's Gambit Exchange Variation is calm."
    assert _found(text, "Queen's Gambit", _CARO_KANN_EXCHANGE) == [
        ("Queen's Gambit Exchange Variation", None),
    ]


def test_find_short_form_equal_to_another_openings_full_name_is_not_linked():
    text = "Black tried the St. George Defense."
    assert _found(text, "Zukertort Opening: St. George Defense") == [
        ("St. George Defense", None),
    ]


def test_find_short_form_is_case_sensitive():
    assert _found("Follow the main line here.", _BENKO_MAIN_LINE) == []
    assert _found("Follow the Main Line here.", _BENKO_MAIN_LINE) == [
        ("Main Line", _BENKO_MAIN_LINE),
    ]


# --- nearest (move-tree proximity ranking) --------------------------------


def test_nearest_surfaces_relevant_lines_over_sidelines():
    """For 1.e4 c5 2.Nf3 d6 every result is a genuine neighbor of the played
    line (shares the 1.e4 c5 2.Nf3 stem) -- the immediate move-3 branches.
    The 2.x sidelines (Bowdler/Amazon, sharing only 2 plies) must not crowd
    them out -- the alphabetical-slice bug this ranking replaced."""
    book = OpeningBook.load()
    line = ["e2e4", "c7c5", "g1f3", "d7d6"]
    rows = book.nearest(line, limit=8)
    names = [o.name for o in rows]
    assert not any(("Bowdler" in n or "Amazon" in n) for n in names), names
    assert all(o.moves[:3] == tuple(line[:3]) for o in rows), names


def test_nearest_top_shares_the_full_line():
    """The closest opening starts with the played moves (shared prefix =
    the whole current line)."""
    book = OpeningBook.load()
    line = ["e2e4", "c7c5", "g1f3", "d7d6"]
    rows = book.nearest(line, limit=8)
    assert rows
    assert rows[0].moves[: len(line)] == tuple(line)


def test_nearest_family_filter_restricts_pool():
    book = OpeningBook.load()
    line = ["e2e4", "c7c5", "g1f3", "d7d6"]  # a Sicilian line
    rows = book.nearest(line, family="Caro-Kann Defense", limit=5)
    assert rows
    assert all(OpeningBook.family_of(o.name) == "Caro-Kann Defense" for o in rows)


def test_nearest_empty_line_falls_back_to_shortest():
    book = OpeningBook.load()
    rows = book.nearest([], limit=3)
    assert len(rows) == 3
    # No proximity signal -> fundamental-first (non-decreasing ply).
    plies = [o.ply for o in rows]
    assert plies == sorted(plies)


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
