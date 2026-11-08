"""Pawn formations (play/formations.py) and their catalog wiring
(docs/ai-playbook-matchers-spec.md, Stage 2).
"""
from __future__ import annotations

import logging
from pathlib import Path

import chess
import pytest

from sturddle_view.llm.playbook import render_playbook
from sturddle_view.llm.prompts import COACH_MODE
from sturddle_view.play.formations import (
    FormationMatcher,
    PatternError,
    example_misses,
    holds,
    parse_pattern,
)
from sturddle_view.play.matchers import Match, MatchContext
from sturddle_view.play.playbook import Situation, classify
from sturddle_view.play.playbook_catalog import SHIPPED_DIR, CatalogError, load_catalog
from sturddle_view.play.playbook_tags import (
    AXIS_FORMATION,
    MARGIN_EVEN,
    MARGIN_WORSE,
    PHASE_MIDDLEGAME,
    PHASE_OPENING,
    SOURCE_EVAL,
)

_STONEWALL_FEN = "rnbqkb1r/ppp2ppp/4pn2/3p4/3P1P2/2P1P3/PP4PP/RNBQKBNR w KQkq - 0 1"
_NO_STONEWALL_FEN = "rnbqkb1r/ppp2ppp/4pn2/3p4/3P4/2P1P3/PP3PPP/RNBQKBNR w KQkq - 0 1"
_HEDGEHOG_FEN = "r2qkb1r/1b3ppp/pp1ppn2/8/2P1P3/2N2N2/PP3PPP/R1BQKB1R b KQkq - 0 1"
# Lone d4 / d5, no c- or e-pawns: fits a symmetric pattern for both sides.
_SYMMETRIC_FEN = "4k3/pp3ppp/8/3p4/3P4/8/PP3PPP/4K3 w - - 0 1"


def _stonewall(**overrides) -> dict:
    raw = dict(
        id="wall",
        ours={"c": "3", "d": "4", "e": "3", "f": "4"},
        examples=[_STONEWALL_FEN],
        counterexamples=[_NO_STONEWALL_FEN],
    )
    raw.update(overrides)
    return raw


def _ctx(board: chess.Board, color: chess.Color, phase: str = PHASE_MIDDLEGAME) -> MatchContext:
    return MatchContext(
        board=board, our_color=color, margin=MARGIN_EVEN, margin_source=SOURCE_EVAL, phase=phase,
    )


def _shipped():
    return load_catalog(SHIPPED_DIR, None)


# --- grammar ---------------------------------------------------------------

def test_parse_reads_every_value_form():
    pattern = parse_pattern(_stonewall(theirs={"a": "*", "h": "2-3", "b": "-"}, examples=[
        "rnbqkb1r/p1p2pp1/4pn1p/3p4/3P1P2/2P1P3/PP4PP/RNBQKBNR w KQkq - 0 1",
    ], counterexamples=[_NO_STONEWALL_FEN]))
    assert {(r.file, r.ranks) for r in pattern.theirs} == {
        (chess.FILE_NAMES.index("a"), frozenset(range(2, 8))),
        (chess.FILE_NAMES.index("b"), frozenset()),
        (chess.FILE_NAMES.index("h"), frozenset({2, 3})),
    }


@pytest.mark.parametrize("overrides, message", [
    ({"ours": {"i": "3", "d": "4", "e": "3"}}, "bad file"),
    ({"ours": {"c": "8", "d": "4", "e": "3"}}, "bad rank"),
    ({"ours": {"c": "1", "d": "4", "e": "3"}}, "bad rank"),
    ({"ours": {"c": "4-3", "d": "4", "e": "3"}}, "inverted range"),
    ({"ours": {"c": 3, "d": "4", "e": "3"}}, "bad rank"),
    ({"ours": {"d": "4", "f": "4"}}, "constraints"),
    ({"id": "Wall"}, "bad pattern id"),
    ({"phases": ["middle"]}, "bad phases"),
    ({"mirror": "yes"}, "boolean"),
    ({"examples": []}, "at least one FEN"),
    ({"examples": ["not a fen"]}, "bad FEN"),
    ({"examples": [_NO_STONEWALL_FEN]}, "example does not fit"),
    ({"counterexamples": [_STONEWALL_FEN]}, "counterexample fits"),
    ({"wing": "king"}, "bad pattern fields"),
])
def test_parse_rejects(overrides, message):
    with pytest.raises(PatternError, match=message):
        parse_pattern(_stonewall(**overrides))


def test_ranks_are_relative_to_each_side():
    pattern = parse_pattern(_stonewall())
    flipped = chess.Board(_STONEWALL_FEN).mirror()
    assert holds(pattern, flipped, chess.BLACK)
    assert not holds(pattern, flipped, chess.WHITE)


def test_mirror_is_opt_in():
    # Stonewall on the other wing: f3 e4 d3 c4.
    board = chess.Board("rnbqkb1r/ppp2ppp/4pn2/3p4/2P1P3/3P1P2/PP4PP/RNBQKBNR w KQkq - 0 1")
    assert not holds(parse_pattern(_stonewall()), board, chess.WHITE)
    assert holds(parse_pattern(_stonewall(mirror=True)), board, chess.WHITE)


# --- matcher ---------------------------------------------------------------

def test_matcher_tags_both_points_of_view():
    matcher = FormationMatcher((parse_pattern(_stonewall()),))
    board = chess.Board(_STONEWALL_FEN)
    assert matcher.match(_ctx(board, chess.WHITE)) == (Match(AXIS_FORMATION, "wall_ours"),)
    assert matcher.match(_ctx(board, chess.BLACK)) == (Match(AXIS_FORMATION, "wall_theirs"),)


def test_matcher_respects_phases():
    matcher = FormationMatcher((parse_pattern(_stonewall(phases=[PHASE_OPENING])),))
    assert matcher.match(_ctx(chess.Board(_STONEWALL_FEN), chess.WHITE)) == ()


def test_matcher_skips_a_pattern_that_fits_both_sides():
    symmetric = parse_pattern(dict(
        id="lone_d",
        ours={"c": "-", "d": "4", "e": "-"},
        examples=["4k3/pp3ppp/8/8/3P4/8/PP3PPP/4K3 w - - 0 1"],
        counterexamples=["4k3/pp3ppp/8/8/8/8/PP3PPP/4K3 w - - 0 1"],
    ))
    matcher = FormationMatcher((symmetric,))
    assert matcher.match(_ctx(chess.Board(_SYMMETRIC_FEN), chess.WHITE)) == ()


def test_most_constraints_wins_then_load_order():
    loose = parse_pattern(_stonewall(id="loose", ours={"c": "3", "d": "4", "f": "4"}))
    tight = parse_pattern(_stonewall(id="tight"))
    twin = parse_pattern(_stonewall(id="twin"))
    board = chess.Board(_STONEWALL_FEN)
    assert FormationMatcher((loose, tight)).match(_ctx(board, chess.WHITE))[0].tag == "tight_ours"
    assert FormationMatcher((tight, twin)).match(_ctx(board, chess.WHITE))[0].tag == "tight_ours"


def test_example_misses_catches_a_stolen_example():
    loose = parse_pattern(_stonewall(id="loose", ours={"c": "3", "d": "4", "f": "4"}))
    tight = parse_pattern(_stonewall(id="tight"))
    assert example_misses(FormationMatcher((tight,))) == []
    assert example_misses(FormationMatcher((loose, tight)))  # tight steals loose's example


def test_hedgehog_refines_maroczy_from_both_sides():
    catalog = _shipped()
    board = chess.Board(_HEDGEHOG_FEN)
    for color, tag in ((chess.BLACK, "hedgehog_ours"), (chess.WHITE, "hedgehog_theirs")):
        situation = classify(board, score_white=None, our_color=color, matchers=catalog.matchers)
        assert situation.formation == tag



def test_french_chain_wins_the_tie_with_a_stonewall_against_e5():
    # Black c6 d5 e6 f5 against White d4 e5: the Stonewall hole holds a pawn.
    board = chess.Board("rnbqkbnr/pp4pp/2p1p3/3pPp2/3P4/8/PPP2PPP/RNBQKBNR w KQkq - 0 1")
    matchers = _shipped().matchers
    for color, tag in ((chess.WHITE, "french_chain_ours"), (chess.BLACK, "french_chain_theirs")):
        assert classify(board, score_white=None, our_color=color, matchers=matchers).formation == tag


def test_benoni_is_not_a_minority_attack():
    board = chess.Board("rnbq1rk1/pp3pbp/3p1np1/2pP4/4P3/2N2N2/PP2BPPP/R1BQK2R w KQ - 0 9")
    situation = classify(board, score_white=None, our_color=chess.WHITE, matchers=_shipped().matchers)
    assert (situation.formation, situation.imbalance) == ("benoni_ours", None)


# --- catalog ---------------------------------------------------------------

def test_static_registry_has_no_formations():
    board = chess.Board(_STONEWALL_FEN)
    assert classify(board, score_white=None, our_color=chess.WHITE).formation is None
    assert classify(
        board, score_white=None, our_color=chess.WHITE, matchers=_shipped().matchers,
    ).formation == "stonewall_ours"


def test_composed_registry_has_one_matcher_per_axis():
    axes = [axis for matcher in _shipped().matchers for axis in matcher.tags]
    assert len(axes) == len(set(axes))
    assert set(axes) <= set(Situation.__dataclass_fields__)


def _toml_pattern(pattern_id: str, ours: str, example: str, counter: str) -> str:
    return (
        f'[[pattern]]\nid = "{pattern_id}"\nours = {ours}\n'
        f'examples = ["{example}"]\ncounterexamples = ["{counter}"]\n'
    )


def _toml_fragment(tag: str, text: str) -> str:
    return f'[[entry]]\nid = "formation.{tag}"\nwhen = {{ formation = "{tag}" }}\ntext = "{text}"\n'


# White c3 d4 e3 f4 g3: a stonewall with a g3 pawn.
_WALL_G3_FEN = "rnbqkb1r/ppp2ppp/4pn2/3p4/3P1P2/2P1P1P1/PP5P/RNBQKBNR w KQkq - 0 1"
_WALL_G3_RULES = '{ c = "3", d = "4", e = "3", f = "4", g = "3" }'
_WALL_RULES = '{ c = "3", d = "4", e = "3", f = "4" }'


def _write(dir_: Path, name: str, body: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / name).write_text(body, encoding="utf-8")


def test_user_pattern_and_fragments_across_files(tmp_path):
    user = tmp_path / "user"
    _write(user, "a.toml", _toml_fragment("lean_ours", "lean ours.") + _toml_fragment(
        "lean_theirs", "lean theirs.",
    ))
    _write(user, "b.toml", _toml_pattern(
        "lean", '{ a = "-", b = "-", c = "-", d = "-" }',
        "4k3/pppp4/8/8/8/8/4PPPP/4K3 w - - 0 1", "4k3/pppp4/8/8/8/8/P3PPP1/4K3 w - - 0 1",
    ))
    catalog = load_catalog(SHIPPED_DIR, user)
    assert [p.id for p in catalog.patterns][-1] == "lean"
    assert catalog.fragment(AXIS_FORMATION, "lean_ours") == "lean ours."


def test_user_pattern_missing_a_fragment_is_dropped_with_the_other(tmp_path, caplog):
    user = tmp_path / "user"
    _write(user, "a.toml", _toml_pattern(
        "lean", '{ a = "-", b = "-", c = "-", d = "-" }',
        "4k3/pppp4/8/8/8/8/4PPPP/4K3 w - - 0 1", "4k3/pppp4/8/8/8/8/P3PPP1/4K3 w - - 0 1",
    ) + _toml_fragment("lean_ours", "lean ours."))
    with caplog.at_level(logging.WARNING):
        catalog = load_catalog(SHIPPED_DIR, user)
    assert "lean" not in [p.id for p in catalog.patterns]
    assert "formation.lean_ours" not in catalog.entries
    assert "needs fragments" in caplog.text


def test_user_pattern_cannot_redefine_a_shipped_id(tmp_path, caplog):
    user = tmp_path / "user"
    _write(user, "a.toml", _toml_pattern(
        "stonewall", _WALL_G3_RULES, _WALL_G3_FEN, _NO_STONEWALL_FEN,
    ))
    with caplog.at_level(logging.WARNING):
        catalog = load_catalog(SHIPPED_DIR, user)
    assert [p.id for p in catalog.patterns] == [p.id for p in _shipped().patterns]
    assert "cannot be redefined" in caplog.text


def test_user_pattern_cannot_steal_a_shipped_example(tmp_path, caplog):
    # More constraints than the shipped stonewall and fits its example.
    user = tmp_path / "user"
    _write(user, "a.toml", _toml_pattern(
        "tall_wall", '{ a = "2", c = "3", d = "4", e = "3", f = "4" }',
        _STONEWALL_FEN, _NO_STONEWALL_FEN,
    ) + _toml_fragment("tall_wall_ours", "x.") + _toml_fragment("tall_wall_theirs", "y."))
    with caplog.at_level(logging.WARNING):
        catalog = load_catalog(SHIPPED_DIR, user)
    assert "tall_wall" not in [p.id for p in catalog.patterns]
    assert "mis-tagged" in caplog.text


def test_user_pattern_loses_a_tie_to_a_shipped_one(tmp_path):
    # Same four constraints as the shipped stonewall: shipped wins the tie,
    # so the user pattern's own example (same FEN) is the shipped tag.
    user = tmp_path / "user"
    _write(user, "a.toml", _toml_pattern("my_wall", _WALL_RULES, _STONEWALL_FEN, _NO_STONEWALL_FEN))
    catalog = load_catalog(SHIPPED_DIR, user)
    assert "my_wall" not in [p.id for p in catalog.patterns]


def test_shipped_pattern_errors_raise(tmp_path):
    _write(tmp_path, "a.toml", _toml_pattern("wall", '{ d = "4" }', _STONEWALL_FEN, _NO_STONEWALL_FEN))
    with pytest.raises(CatalogError, match="constraints"):
        load_catalog(tmp_path, None)


def test_shipped_overlap_raises(tmp_path):
    _write(tmp_path, "a.toml", _toml_pattern(
        "loose", '{ c = "3", d = "4", f = "4" }', _STONEWALL_FEN, _NO_STONEWALL_FEN,
    ) + _toml_pattern("tight", _WALL_RULES, _STONEWALL_FEN, _NO_STONEWALL_FEN))
    with pytest.raises(CatalogError, match="mis-tagged"):
        load_catalog(tmp_path, None)


# --- rendering -------------------------------------------------------------

def test_formation_renders_after_imbalance_before_bishops():
    catalog = _shipped()
    situation = Situation(
        margin=MARGIN_WORSE, margin_source=SOURCE_EVAL, phase=PHASE_MIDDLEGAME,
        imbalance="iqp_ours", formation="stonewall_ours", bishops="opposite",
    )
    line = render_playbook(situation, COACH_MODE, chess.WHITE, catalog)
    formation = catalog.fragment(AXIS_FORMATION, "stonewall_ours")
    # Margin + imbalance + formation fill the cap; bishops drops.
    assert line.index(catalog.fragment("imbalance", "iqp_ours")) < line.index(formation)
    assert catalog.fragment("bishops", "opposite") not in line
