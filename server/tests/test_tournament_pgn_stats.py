"""Slice 2: pgn_stats — PGN-driven standings, Elo, and SPRT.

Fixture PGNs are hand-crafted strings inside the tests; no external
PGN files. Each test writes to a tmp file because the public API takes
a Path."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from sturddle_view.tournament.pgn_stats import (
    Standings,
    SprtResult,
    compute_sprt,
    compute_standings,
    elo_from_score,
    elo_margin_from_wld,
)


def _write_pgn(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "games.pgn"
    p.write_text(body, encoding="utf-8")
    return p


def _game(white: str, black: str, result: str) -> str:
    return (
        f'[Event "x"]\n[White "{white}"]\n[Black "{black}"]\n'
        f'[Result "{result}"]\n\n1. e4 e5 {result}\n\n'
    )


def _game_round(round_tag: str, white: str, black: str, result: str) -> str:
    return (
        f'[Event "x"]\n[Round "{round_tag}"]\n[White "{white}"]\n'
        f'[Black "{black}"]\n[Result "{result}"]\n\n1. e4 e5 {result}\n\n'
    )


# ---------------------------------------------------------------------------
# Standings
# ---------------------------------------------------------------------------


def test_standings_missing_pgn_returns_empty(tmp_path):
    s = compute_standings(tmp_path / "absent.pgn")
    assert s.games == 0
    assert s.engines == []


def test_standings_empty_pgn_returns_empty(tmp_path):
    p = _write_pgn(tmp_path, "")
    s = compute_standings(p)
    assert s.games == 0
    assert s.engines == []


def test_standings_single_white_win(tmp_path):
    p = _write_pgn(tmp_path, _game("A", "B", "1-0"))
    s = compute_standings(p)
    assert s.games == 1
    by = {e.name: e for e in s.engines}
    assert by["A"].wins == 1 and by["A"].losses == 0 and by["A"].draws == 0
    assert by["B"].wins == 0 and by["B"].losses == 1 and by["B"].draws == 0


def test_standings_single_black_win(tmp_path):
    p = _write_pgn(tmp_path, _game("A", "B", "0-1"))
    s = compute_standings(p)
    by = {e.name: e for e in s.engines}
    assert by["A"].losses == 1
    assert by["B"].wins == 1


def test_standings_draws(tmp_path):
    p = _write_pgn(tmp_path, _game("A", "B", "1/2-1/2"))
    s = compute_standings(p)
    by = {e.name: e for e in s.engines}
    assert by["A"].draws == 1 and by["B"].draws == 1
    assert by["A"].points == 0.5
    assert by["A"].score_pct == 0.5


def test_standings_skips_unfinished_games(tmp_path):
    body = _game("A", "B", "1-0") + _game("A", "B", "*") + _game("A", "B", "0-1")
    p = _write_pgn(tmp_path, body)
    s = compute_standings(p)
    assert s.games == 2  # the * game was skipped


def test_standings_dedups_resume_duplicate_keeps_last(tmp_path):
    # Simulates a resume duplicate: round 2 (A vs B) appears twice — the
    # first entry was the killed-mid-pair game whose result fastchess never
    # recorded in cfg.json, so it replayed it on resume. Standings must
    # count the *last* one only.
    body = (
        _game_round("1", "A", "B", "1-0")
        + _game_round("2", "A", "B", "1-0")  # killed; result superseded
        + _game_round("2", "A", "B", "0-1")  # resume replay; this counts
        + _game_round("2", "B", "A", "1/2-1/2")  # other side of pair
    )
    p = _write_pgn(tmp_path, body)
    s = compute_standings(p)
    assert s.games == 3
    by = {e.name: e for e in s.engines}
    # A: 1 win (R1) + 1 loss (R2) + 1 draw (R2 reverse) = W1 L1 D1
    assert (by["A"].wins, by["A"].losses, by["A"].draws) == (1, 1, 1)
    assert (by["B"].wins, by["B"].losses, by["B"].draws) == (1, 1, 1)


def test_standings_no_dedup_when_round_absent(tmp_path):
    # python-chess fills missing Round with "?"; we must NOT collapse those,
    # otherwise hand-crafted PGNs (and the rest of this test file) break.
    body = _game("A", "B", "1-0") + _game("A", "B", "1-0")
    p = _write_pgn(tmp_path, body)
    s = compute_standings(p)
    assert s.games == 2


def test_standings_round_robin_three_engines(tmp_path):
    # A beats B, B beats C, C beats A (rock-paper-scissors). All draws otherwise.
    body = (
        _game("A", "B", "1-0")
        + _game("B", "C", "1-0")
        + _game("C", "A", "1-0")
        + _game("A", "B", "1/2-1/2")
        + _game("B", "C", "1/2-1/2")
        + _game("C", "A", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    s = compute_standings(p)
    assert s.games == 6
    by = {e.name: e for e in s.engines}
    for n in ("A", "B", "C"):
        assert by[n].wins == 1
        assert by[n].losses == 1
        assert by[n].draws == 2
        assert by[n].points == 2.0


def test_standings_sort_by_points_desc(tmp_path):
    body = (
        _game("A", "B", "1-0")
        + _game("A", "C", "1-0")
        + _game("B", "C", "1-0")
        + _game("A", "B", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p).to_dict()
    names = [e["name"] for e in d["engines"]]
    assert names == ["A", "B", "C"]


def test_standings_to_dict_includes_elo(tmp_path):
    body = _game("A", "B", "1-0") + _game("A", "B", "1-0") + _game("A", "B", "0-1")
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p).to_dict()
    a = next(e for e in d["engines"] if e["name"] == "A")
    # 2 wins out of 3 → score 2/3 → +120.something Elo
    assert a["elo"] is not None
    assert 100 < a["elo"] < 150
    assert a["elo_margin_95"] is not None
    assert a["elo_margin_95"] > 0


def test_standings_elo_omitted_for_three_or_more_engines(tmp_path):
    # With N≥3 the per-engine score% is "vs field" (mixed strengths),
    # not a head-to-head Elo — both elo and elo_margin_95 must be None.
    body = (
        _game("A", "B", "1-0") + _game("A", "C", "1-0") + _game("B", "C", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p).to_dict()
    for e in d["engines"]:
        assert e["elo"] is None
        assert e["elo_margin_95"] is None


def test_elo_margin_from_wld_perfect_score_is_none():
    assert elo_margin_from_wld(5, 0, 0) is None
    assert elo_margin_from_wld(0, 5, 0) is None


def test_elo_margin_from_wld_under_two_games_is_none():
    assert elo_margin_from_wld(0, 0, 0) is None
    assert elo_margin_from_wld(1, 0, 0) is None


def test_elo_margin_from_wld_shrinks_with_more_games():
    # Same score% (50%), more games → tighter CI.
    small = elo_margin_from_wld(5, 5, 0)
    large = elo_margin_from_wld(50, 50, 0)
    assert small is not None and large is not None
    assert large < small


def test_elo_from_score_perfect_score_is_none():
    assert elo_from_score(1.0) is None
    assert elo_from_score(0.0) is None


def test_elo_from_score_50_pct_is_zero():
    assert elo_from_score(0.5) == pytest.approx(0.0)


def test_elo_from_score_known_values():
    # +400 Elo ⇨ score 10/11 ≈ 0.9091
    assert elo_from_score(10 / 11) == pytest.approx(400.0, abs=0.5)
    # -400 Elo ⇨ score 1/11
    assert elo_from_score(1 / 11) == pytest.approx(-400.0, abs=0.5)


# ---------------------------------------------------------------------------
# SPRT
# ---------------------------------------------------------------------------


def _params(**overrides) -> dict:
    p = {"elo0": 0.0, "elo1": 5.0, "alpha": 0.05, "beta": 0.05, "model": "normalized"}
    p.update(overrides)
    return p


def test_sprt_no_games_returns_continue(tmp_path):
    p = _write_pgn(tmp_path, "")
    r = compute_sprt(p, _params())
    assert r.status == "continue"
    assert r.pairs == 0
    assert r.llr == 0.0


def test_sprt_one_game_returns_continue(tmp_path):
    p = _write_pgn(tmp_path, _game("A", "B", "1-0"))
    r = compute_sprt(p, _params())
    assert r.status == "continue"


def test_sprt_bounds_have_correct_signs():
    # Wald bounds: lower < 0 < upper for typical alpha=beta=0.05.
    r = compute_sprt(Path("/dev/null"), _params())
    assert r.lower_bound < 0 < r.upper_bound
    assert r.lower_bound == pytest.approx(math.log(0.05 / 0.95))
    assert r.upper_bound == pytest.approx(math.log(0.95 / 0.05))


def test_sprt_runaway_a_dominates_accepts_h1(tmp_path):
    # 100 pairs, A wins both games of every pair. Strong H1.
    body = ""
    for _ in range(100):
        body += _game("A", "B", "1-0")  # game 1: A white wins
        body += _game("B", "A", "0-1")  # game 2: A black wins
    p = _write_pgn(tmp_path, body)
    r = compute_sprt(p, _params(elo0=0, elo1=5))
    assert r.pairs == 100
    assert r.status == "H1"
    assert r.llr > r.upper_bound


def test_sprt_runaway_b_dominates_accepts_h0(tmp_path):
    # 100 pairs, A loses both games of every pair. Strongly rejects H1.
    body = ""
    for _ in range(100):
        body += _game("A", "B", "0-1")
        body += _game("B", "A", "1-0")
    p = _write_pgn(tmp_path, body)
    r = compute_sprt(p, _params(elo0=0, elo1=5))
    assert r.pairs == 100
    assert r.status == "H0"
    assert r.llr < r.lower_bound


def test_sprt_balanced_play_continues(tmp_path):
    # Realistic mix: pair scores spread {0, 0.5, 1, 1.5, 2} centered at 1.0.
    # Mean ≈ 1.0 (50% per game), real variance > 0. With elo0=0, elo1=5
    # and only ~12 pairs, LLR should sit between Wald bounds.
    body = ""
    pair_outcomes = [
        ("1-0", "0-1"),   # split → A scores 1
        ("1-0", "1-0"),   # A wins both → 2
        ("0-1", "0-1"),   # B wins both → 0
        ("1/2-1/2", "1/2-1/2"),   # both drawn → 1
        ("1-0", "1/2-1/2"),       # 1.5
        ("0-1", "1/2-1/2"),       # 0.5
        # Repeat the symmetric set so mean stays ≈ 1.0.
        ("0-1", "1-0"),   # 1
        ("1-0", "1-0"),   # 2
        ("0-1", "0-1"),   # 0
        ("1/2-1/2", "1/2-1/2"),   # 1
        ("1/2-1/2", "1-0"),       # 1.5
        ("1/2-1/2", "0-1"),       # 0.5
    ]
    for r1, r2 in pair_outcomes:
        body += _game("A", "B", r1) + _game("B", "A", r2)
    p = _write_pgn(tmp_path, body)
    r = compute_sprt(p, _params(elo0=0, elo1=5))
    assert r.status == "continue", (
        f"expected continue with balanced play, got {r.status} (LLR={r.llr})"
    )


def test_sprt_drops_trailing_odd_game(tmp_path):
    # 3 games → 1 complete pair, last game dropped.
    body = _game("A", "B", "1-0") + _game("B", "A", "0-1") + _game("A", "B", "1-0")
    p = _write_pgn(tmp_path, body)
    r = compute_sprt(p, _params())
    assert r.pairs == 1


def test_sprt_unimplemented_model_raises(tmp_path):
    p = _write_pgn(tmp_path, "")
    with pytest.raises(NotImplementedError):
        compute_sprt(p, _params(model="bayesian"))


def test_sprt_to_dict_round_trip(tmp_path):
    p = _write_pgn(tmp_path, "")
    d = compute_sprt(p, _params()).to_dict()
    assert set(d.keys()) == {
        "llr", "lower_bound", "upper_bound", "status",
        "pairs", "elo0", "elo1", "model",
    }
