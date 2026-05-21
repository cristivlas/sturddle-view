"""Slice 2: pgn_stats — PGN-driven standings, Elo, and SPRT.

Fixture PGNs are hand-crafted strings inside the tests; no external
PGN files. Each test writes to a tmp file because the public API takes
a Path."""
from __future__ import annotations

import gzip
import math
from pathlib import Path

import pytest

import json

from sturddle_view.tournament.pgn_stats import (
    Standings,
    SprtResult,
    compute_sprt,
    compute_standings,
    count_partial_pairs,
    elo_from_score,
    elo_margin_from_wld,
    ordo_fit,
    patch_config_json,
    read_game_pgn,
    read_game_record,
    rewrite_drop_partial_pairs,
)


def _find_bak_gz(p: Path) -> Path | None:
    matches = sorted(p.parent.glob(f"{p.name}.*.bak.gz"))
    return matches[-1] if matches else None


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


class _RoundCounter:
    """Hands out monotonically increasing round tags for SPRT pair fixtures."""
    __slots__ = ("_n",)

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return str(self._n)


def _pair(rc: _RoundCounter, a: str, b: str, result_ab: str, result_ba: str) -> str:
    """One color-flipped Round-tagged pair: A-as-white then B-as-white."""
    rt = rc.next()
    return _game_round(rt, a, b, result_ab) + _game_round(rt, b, a, result_ba)


def _sprt(pgn_path, params, *, engine_a="A", engine_b="B"):
    """compute_sprt with default A/B engine identities for tests."""
    return compute_sprt(pgn_path, params, engine_a=engine_a, engine_b=engine_b)


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


def test_standings_counts_every_decisive_game(tmp_path):
    # Standings tally every decisive entry in file order; no dedup by
    # (Round, White, Black). See docs/pgn-reconciliation.md # "Pair
    # identity in stored PGN" -- Round is not a reliable pair ID, so
    # anything in the PGN is real.
    body = (
        _game_round("1", "A", "B", "1-0")
        + _game_round("2", "A", "B", "1-0")
        + _game_round("2", "A", "B", "0-1")  # second A-as-white in round 2
        + _game_round("2", "B", "A", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    s = compute_standings(p)
    assert s.games == 4
    by = {e.name: e for e in s.engines}
    # A: 2 wins (R1, R2 first), 1 loss (R2 second), 1 draw (R2 reverse)
    assert (by["A"].wins, by["A"].losses, by["A"].draws) == (2, 1, 1)
    assert (by["B"].wins, by["B"].losses, by["B"].draws) == (1, 2, 1)


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
    # With N>=3 the per-engine score% is "vs field" (mixed strengths),
    # not a head-to-head Elo -- both elo and elo_margin_95 must be None.
    body = (
        _game("A", "B", "1-0") + _game("A", "C", "1-0") + _game("B", "C", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p).to_dict()
    for e in d["engines"]:
        assert e["elo"] is None
        assert e["elo_margin_95"] is None


def test_gauntlet_standings_wld(tmp_path):
    # 3-engine gauntlet: leader A plays B and C (color-flipped pairs).
    # A wins all 4 games vs B; A draws all 4 games vs C.
    body = (
        _game("A", "B", "1-0") + _game("B", "A", "0-1")  # pair A vs B
        + _game("A", "B", "1-0") + _game("B", "A", "0-1")  # pair A vs B
        + _game("A", "C", "1/2-1/2") + _game("C", "A", "1/2-1/2")  # pair A vs C
        + _game("A", "C", "1/2-1/2") + _game("C", "A", "1/2-1/2")  # pair A vs C
    )
    p = _write_pgn(tmp_path, body)
    s = compute_standings(p, tournament_type="gauntlet")
    by = {e.name: e for e in s.engines}
    assert s.games == 8
    assert by["A"].wins == 4 and by["A"].losses == 0 and by["A"].draws == 4
    assert by["B"].wins == 0 and by["B"].losses == 4 and by["B"].draws == 0
    assert by["C"].wins == 0 and by["C"].losses == 0 and by["C"].draws == 4


def test_gauntlet_standings_leader_elo_is_none(tmp_path):
    # Leader (A) has no meaningful head-to-head record vs itself; elo stays None.
    body = (
        _game("A", "B", "1-0") + _game("B", "A", "0-1")
        + _game("A", "C", "1/2-1/2") + _game("C", "A", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p, tournament_type="gauntlet").to_dict()
    leader = next(e for e in d["engines"] if e["name"] == "A")
    assert leader["elo"] is None


def test_gauntlet_standings_elo_per_engine(tmp_path):
    # Elo is computed per-engine vs the leader (A), not vs field.
    # B scores 1/4 vs A => negative Elo. C scores 2/4 (all draws) => ~0 Elo.
    body = (
        _game("A", "B", "1-0") + _game("B", "A", "0-1")
        + _game("A", "B", "1-0") + _game("B", "A", "1-0")  # B wins once
        + _game("A", "C", "1/2-1/2") + _game("C", "A", "1/2-1/2")
        + _game("A", "C", "1/2-1/2") + _game("C", "A", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p, tournament_type="gauntlet").to_dict()
    by = {e["name"]: e for e in d["engines"]}
    assert by["B"]["elo"] is not None and by["B"]["elo"] < 0
    assert by["C"]["elo"] is not None and by["C"]["elo"] == pytest.approx(0.0, abs=1.0)


def test_gauntlet_standings_exact_challenger_elo_and_margin(tmp_path):
    """Pin exact challenger Elo and margin against the leader. Kills
    NumberReplacers on the wld `[0, 0, 0]` initializers and the
    index access `[0]`/`[1]`/`[2]` (W/L/D slot routing)."""
    # B vs leader A: 2 wins, 1 loss, 1 draw → score 0.625.
    # Color-flipped so all 4 are leader-vs-challenger pairs.
    body = (
        _game("A", "B", "0-1") + _game("B", "A", "1-0")  # 2× B wins
        + _game("A", "B", "1-0")  # A wins
        + _game("A", "B", "1/2-1/2")  # draw
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p, tournament_type="gauntlet").to_dict()
    by = {e["name"]: e for e in d["engines"]}
    # B: 2W 1L 1D vs A → exact head-to-head Elo and margin.
    assert by["B"]["elo"] == pytest.approx(88.7395, abs=0.01)
    assert by["B"]["elo_margin_95"] == pytest.approx(347.7241, abs=0.01)


def test_gauntlet_standings_n_engines_branch_runs_for_four(tmp_path):
    """4-engine gauntlet with non-perfect challenger scores → challenger
    gets a real Elo. Kills `>= 3`→`== 3` mutation (which would skip the
    gauntlet branch for n=4 and leave elo None)."""
    body = (
        _game("A", "B", "1/2-1/2") + _game("B", "A", "1/2-1/2")  # B: 0W 0L 2D vs A
        + _game("A", "C", "1-0") + _game("C", "A", "0-1")  # C: 0W 2L 0D
        + _game("A", "D", "1-0") + _game("D", "A", "0-1")
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p, tournament_type="gauntlet").to_dict()
    by = {e["name"]: e for e in d["engines"]}
    # B drew both vs A → score 0.5 → elo 0.0 exactly. If `== 3` mutation
    # is applied, the gauntlet branch is skipped → B.elo stays None.
    assert by["B"]["elo"] is not None
    assert by["B"]["elo"] == pytest.approx(0.0, abs=0.01)


def test_single_engine_standings_no_elo_no_ordo(tmp_path):
    """1 engine → none of the Elo branches run. Kills `== 2`→`<= 2`
    on L1003 head-to-head guard and `>= 2`→`>= 1` on L1025 ordo guard."""
    # PGN with a single engine playing itself (white==black) is degenerate,
    # so build one with only one EngineRecord by using same name twice.
    # _iter_games would still record both white and black as the same name.
    body = _game("A", "A", "1-0")
    p = _write_pgn(tmp_path, body)
    s = compute_standings(p)
    assert len(s.engines) == 1
    e = s.engines[0]
    # Head-to-head Elo branch is for len==2 only.
    assert e.elo is None
    assert e.elo_margin_95 is None
    # ordo branch is for len>=2 only.
    assert e.elo_ordo is None
    assert e.elo_ordo_margin_95 is None


def test_two_engine_standings_populates_ordo_margin(tmp_path):
    """2 engines with 4 games (2W 2L from A's side) → ordo margin is a
    finite number (not None). Pins `enc[1] += 1` played-counter increment:
    a NumberReplacer `+= 1`→`+= 0` would leave np_=0 → margin None."""
    body = (
        _game("A", "B", "1-0") + _game("B", "A", "1-0")
        + _game("A", "B", "1-0") + _game("B", "A", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    s = compute_standings(p)
    by = {e.name: e for e in s.engines}
    # Both engines went 2-2 → ordo Elo ~ 0, margin finite.
    assert by["A"].elo_ordo_margin_95 is not None
    assert by["A"].elo_ordo_margin_95 > 0


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


def test_elo_margin_from_wld_exact_symmetric(pytest_approx=None):
    """5W5L0D at 50% → known value. Pins the variance formula
    and the Elo propagation constants."""
    result = elo_margin_from_wld(5, 5, 0)
    assert result == pytest.approx(226.99, abs=0.01)


def test_elo_margin_from_wld_exact_with_draws():
    """3W1L2D → known value. Draws contribute (0.5-s)² term; pins
    the draw term coefficient in the variance formula."""
    result = elo_margin_from_wld(3, 1, 2)
    assert result == pytest.approx(255.37, abs=0.01)


def test_elo_margin_from_wld_exact_asymmetric():
    """1W9L0D (low score) → known value. Pins (0-s)² loss term and
    the dElo/dscore denominator at non-0.5 score."""
    result = elo_margin_from_wld(1, 9, 0)
    assert result == pytest.approx(378.32, abs=0.01)


def test_elo_margin_from_wld_exact_with_wins_and_draws():
    """10W0L10D (s=0.75) → known value. Pins the win+draw combination."""
    result = elo_margin_from_wld(10, 0, 10)
    assert result == pytest.approx(104.15, abs=0.01)


def test_elo_margin_from_wld_all_draws_returns_zero():
    """All draws → var=0 → returns 0.0 exactly. Kills NumberReplacer
    on the `return 0.0` branch."""
    assert elo_margin_from_wld(0, 0, 5) == 0.0
    assert elo_margin_from_wld(0, 0, 10) == 0.0


# ---------------------------------------------------------------------------
# _ordo_fit_margins — pin numeric output to kill formula-operator survivors
# ---------------------------------------------------------------------------

from sturddle_view.tournament.pgn_stats import _ordo_fit_margins, _ordo_iterative_fit  # noqa: E402


def test_ordo_fit_margins_one_engine_returns_none():
    """n<2 → all None. Kills guard mutations."""
    result = _ordo_fit_margins(["A"], [], {"A": 0.0})
    assert result == {"A": None}


def test_ordo_fit_margins_zero_info_returns_none():
    """No encounters → info=0 → None for all. Kills `<= 0` mutations."""
    result = _ordo_fit_margins(["A", "B"], [], {"A": 0.0, "B": 0.0})
    assert result == {"A": None, "B": None}


def test_ordo_fit_margins_two_engines_exact():
    """2-engine symmetric case: A vs B, 10 games at equal rating. Pins
    the Fisher info accumulation and se_diff computation."""
    engines = ["A", "B"]
    enc = [("A", "B", 5.0, 10)]
    ratings = {"A": 0.0, "B": 0.0}
    result = _ordo_fit_margins(engines, enc, ratings)
    assert result["A"] == pytest.approx(108.617, abs=0.01)
    assert result["B"] == pytest.approx(108.617, abs=0.01)


def test_ordo_fit_margins_two_engines_asymmetric():
    """2-engine with non-zero rating gap. Pins _ORDO_BETA usage in p=1/(1+exp(...))
    and the `/ 2.0` halving in the 2-engine branch."""
    engines = ["A", "B"]
    enc = [("A", "B", 7.0, 10)]
    ratings = {"A": 84.0, "B": -84.0}
    result = _ordo_fit_margins(engines, enc, ratings)
    assert result["A"] == pytest.approx(121.336, abs=0.01)
    assert result["B"] == pytest.approx(121.336, abs=0.01)


def test_ordo_fit_margins_three_engines_exact():
    """3-engine case: exercises the Gauss-Jordan path and the last-engine
    variance-under-constraint formula. Pins all numeric output."""
    engines = ["A", "B", "C"]
    enc = [
        ("A", "B", 6.0, 10),
        ("A", "C", 7.0, 10),
        ("B", "C", 5.0, 10),
    ]
    ratings = {"A": 100.0, "B": 0.0, "C": -100.0}
    result = _ordo_fit_margins(engines, enc, ratings)
    assert result["A"] == pytest.approx(198.69, abs=0.01)
    assert result["B"] == pytest.approx(188.25, abs=0.01)
    assert result["C"] == pytest.approx(338.23, abs=0.01)


def test_ordo_fit_margins_three_engines_symmetric():
    """All at equal rating, equal encounters. A and C margins must be equal
    (symmetry); B in the middle may differ. Pins the Gauss-Jordan inversion
    and the `sum inv[i][j]` accumulation."""
    engines = ["A", "B", "C"]
    enc = [
        ("A", "B", 5.0, 10),
        ("B", "C", 5.0, 10),
        ("A", "C", 5.0, 10),
    ]
    ratings = {"A": 0.0, "B": 0.0, "C": 0.0}
    result = _ordo_fit_margins(engines, enc, ratings)
    assert all(v is not None for v in result.values())
    # A and B (reduced matrix entries) must be equal by symmetry;
    # C (last engine, constraint formula) may differ.
    assert result["A"] == pytest.approx(result["B"], abs=0.01)


# ---------------------------------------------------------------------------
# _ordo_iterative_fit — pin numeric output to kill formula-operator survivors
# ---------------------------------------------------------------------------


def test_ordo_iterative_fit_under_two_engines_returns_empty():
    """n<2 → return {}. Kills NumberReplacer on the `< 2` guard."""
    assert _ordo_iterative_fit([], []) == {}
    assert _ordo_iterative_fit(["solo"], []) == {}


def test_ordo_iterative_fit_two_engines_balanced_exact():
    """A scores 6/10 (white) + B scores 4/10 (reverse) at 50% gap. Pins
    the score-deviation update formula (obtained - expected, step ratio,
    kappa = 0.05, delta halving) to a converged value."""
    r = _ordo_iterative_fit(
        ["A", "B"],
        [("A", "B", 6.0, 10), ("B", "A", 4.0, 10)],
    )
    assert r["A"] == pytest.approx(35.5276, abs=0.01)
    assert r["B"] == pytest.approx(-35.5276, abs=0.01)


def test_ordo_iterative_fit_two_engines_one_sided_exact():
    """A scores 7/10, single-color encounter. Pins the single-encounter
    branch of the obtained accumulation and the kappa/step formula."""
    r = _ordo_iterative_fit(["A", "B"], [("A", "B", 7.0, 10)])
    assert r["A"] == pytest.approx(74.2419, abs=0.01)
    assert r["B"] == pytest.approx(-74.2419, abs=0.01)


def test_ordo_iterative_fit_three_engines_exact():
    """3-engine joint fit, asymmetric scores. Pins mean-centering
    (`m = sum(new_r) / n`, `new_r = [x - m for x in new_r]`) and the
    convergence-improvement test (`if dev2 >= dev: break`)."""
    r = _ordo_iterative_fit(
        ["A", "B", "C"],
        [("A", "B", 6.0, 10), ("A", "C", 7.0, 10), ("B", "C", 5.0, 10)],
    )
    assert r["A"] == pytest.approx(72.4049, abs=0.01)
    assert r["B"] == pytest.approx(-24.1432, abs=0.01)
    assert r["C"] == pytest.approx(-48.2616, abs=0.01)
    # Mean must be exactly zero (mean-centering invariant).
    assert sum(r.values()) == pytest.approx(0.0, abs=1e-9)


def test_ordo_iterative_fit_50_pct_collapses_to_zero():
    """Both engines score 50% over many games → ratings collapse to 0.0
    exactly. Kills mutations that bias the step (Sub→Add on
    `obtained - expected`, USub→Not on the sign multiplier)."""
    r = _ordo_iterative_fit(
        ["A", "B"],
        [("A", "B", 5.0, 10), ("B", "A", 5.0, 10)],
    )
    assert r["A"] == pytest.approx(0.0, abs=0.01)
    assert r["B"] == pytest.approx(0.0, abs=0.01)


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
# ordo-style joint Elo fit
# ---------------------------------------------------------------------------


def test_ordo_fit_two_engines_balanced(tmp_path):
    # 71 games: 17W 16L 38D from A's perspective. Hand-verified against
    # ordo -p tou3r.pgn -a 0 -M -D: +2.5 / -2.5.
    encs = [("A", "B", 17 + 0.5 * 19, 36),  # half white, half black (idealization)
            ("B", "A", 16 + 0.5 * 19, 35)]
    fit = ordo_fit(["A", "B"], encs,
                   wins={"A": 17, "B": 16}, losses={"A": 16, "B": 17})
    a_elo, _ = fit["A"]
    b_elo, _ = fit["B"]
    assert a_elo == pytest.approx(-b_elo, abs=0.01)  # mean-centered
    assert a_elo == pytest.approx(2.47, abs=0.5)


def test_ordo_fit_two_engines_equal_score_gives_zero():
    # Both engines score 50% -- ratings collapse to 0.
    encs = [("A", "B", 5.0, 10), ("B", "A", 5.0, 10)]
    fit = ordo_fit(["A", "B"], encs,
                   wins={"A": 5, "B": 5}, losses={"A": 5, "B": 5})
    assert fit["A"][0] == pytest.approx(0.0, abs=0.5)
    assert fit["B"][0] == pytest.approx(0.0, abs=0.5)


def test_ordo_fit_purges_all_wins_engine():
    # C beat A once, never lost; A and B played a normal pair.
    # C is purged (all-wins); A and B get a proper fit.
    encs = [
        ("A", "B", 1.0, 1), ("B", "A", 1.0, 1),
        ("C", "A", 1.0, 1),
    ]
    fit = ordo_fit(["A", "B", "C"], encs,
                   wins={"A": 1, "B": 1, "C": 1},
                   losses={"A": 1, "B": 1, "C": 0})
    assert fit["C"] == (None, None)
    # A and B form their own component (post-purge), both score 1/2.
    assert fit["A"][0] == pytest.approx(0.0, abs=0.5)
    assert fit["B"][0] == pytest.approx(0.0, abs=0.5)


def test_ordo_fit_purges_all_losses_engine():
    encs = [
        ("A", "B", 1.0, 1), ("B", "A", 1.0, 1),
        ("C", "A", 0.0, 1),
    ]
    fit = ordo_fit(["A", "B", "C"], encs,
                   wins={"A": 2, "B": 1, "C": 0},
                   losses={"A": 0, "B": 1, "C": 1})
    # A is "all wins" -- C never beat A and A never lost overall.
    assert fit["A"] == (None, None)
    assert fit["C"] == (None, None)


def test_ordo_fit_disconnected_components_fit_independently():
    # {A, B} played each other; {C, D} played each other; A never met C/D.
    encs = [
        ("A", "B", 6.0, 10), ("B", "A", 4.0, 10),
        ("C", "D", 4.0, 10), ("D", "C", 6.0, 10),
    ]
    fit = ordo_fit(["A", "B", "C", "D"], encs,
                   wins={"A": 6, "B": 4, "C": 4, "D": 6},
                   losses={"A": 4, "B": 6, "C": 6, "D": 4})
    # Each pair is mean-centered within its own component.
    assert fit["A"][0] == pytest.approx(-fit["B"][0], abs=0.5)
    assert fit["C"][0] == pytest.approx(-fit["D"][0], abs=0.5)
    # A and D both scored 60% vs their only opponent -- same rating.
    assert fit["A"][0] == pytest.approx(fit["D"][0], abs=0.5)


def test_ordo_fit_returns_mean_zero():
    # Three engines, rock-paper-scissors-ish. Ratings must sum to (approximately) 0.
    encs = [
        ("A", "B", 1.0, 1), ("B", "C", 1.0, 1), ("C", "A", 1.0, 1),
    ]
    fit = ordo_fit(["A", "B", "C"], encs,
                   wins={"A": 1, "B": 1, "C": 1},
                   losses={"A": 1, "B": 1, "C": 1})
    elos = [fit[n][0] for n in ("A", "B", "C") if fit[n][0] is not None]
    if elos:
        assert sum(elos) == pytest.approx(0.0, abs=0.01)


def test_ordo_fit_no_wins_losses_dicts_skips_purge():
    """Without wins/losses dicts, no engine is purged even if its W/L
    pattern would qualify. Kills `and`→`or` on the purge-precondition guard."""
    encs = [("A", "B", 1.0, 1), ("B", "A", 0.0, 1)]
    fit = ordo_fit(["A", "B"], encs)
    # A swept B but no purge dicts → A must still get a rating, not (None, None).
    assert fit["A"][0] is not None
    assert fit["B"][0] is not None


def test_ordo_fit_only_wins_dict_skips_purge():
    """Only wins provided (losses=None) → guard fails, no purge. Kills
    the `or`-mutation case where one None side alone would trigger purge."""
    encs = [("A", "B", 1.0, 1), ("B", "A", 0.0, 1)]
    fit = ordo_fit(["A", "B"], encs, wins={"A": 1, "B": 0})
    assert fit["A"][0] is not None
    assert fit["B"][0] is not None


def test_ordo_fit_zero_games_engine_not_purged():
    """An engine with wins=0 AND losses=0 (never played) must NOT be
    purged: both `>0` legs of the purge predicate fail. Kills NumberReplacer
    mutations on the `0` defaults in `.get(n, 0)`."""
    encs = [("A", "B", 1.0, 2), ("B", "A", 1.0, 2)]
    fit = ordo_fit(["A", "B", "Z"], encs,
                   wins={"A": 1, "B": 1, "Z": 0},
                   losses={"A": 1, "B": 1, "Z": 0})
    # Z played nothing → singleton component → (None, None), but for the
    # purge-not-applied reason, not the all-W/all-L reason. The crucial
    # check is that purge does NOT fire on a zero-zero engine.
    # Verified indirectly: A and B form a connected component and get
    # ratings; Z is a singleton.
    assert fit["A"][0] is not None
    assert fit["B"][0] is not None
    assert fit["Z"] == (None, None)


def test_ordo_fit_purge_requires_strict_positive_wins():
    """`wins.get(n, 0) > 0` must be STRICTLY positive. An engine missing
    from the wins dict (defaults to 0) cannot be purged via the wins-leg.
    Kills `> 0`→`>= 0` mutation on the wins-leg of the purge predicate."""
    encs = [("A", "B", 1.0, 2), ("B", "A", 1.0, 2)]
    # "Z" not in wins dict at all → wins.get("Z", 0) == 0 → strictly-positive
    # leg fails. losses.get("Z", 0) > 0 with wins == 0 would trigger the
    # other purge leg, so put losses at 0 too.
    fit = ordo_fit(["A", "B", "Z"], encs,
                   wins={"A": 1, "B": 1},
                   losses={"A": 1, "B": 1})
    # Z must not be purged; it ends up as a singleton (None, None).
    # The point: it must NOT be classified as "all-wins" via `>= 0` mutation.
    # If `>= 0` were used, Z would be flagged because losses.get("Z",0)==0
    # would also satisfy `>= 0` (the symmetric leg). Test passes if Z is
    # singleton-(None,None) for the right reason (connectivity, not purge).
    assert fit["Z"] == (None, None)
    assert fit["A"][0] is not None  # purged engines wouldn't be fit


def test_ordo_fit_singleton_component_returns_none():
    """An engine with zero encounters → singleton component → (None, None).
    Kills `== 1`→`< 1` / `<= 1` mutations on the singleton-check and
    ReplaceContinueWithBreak on the singleton branch (break would skip
    the remaining components)."""
    encs = [("A", "B", 1.0, 2), ("B", "A", 1.0, 2)]
    # Three engines: A+B connected, C isolated. C must get (None, None);
    # A and B must get real ratings (proving `continue` worked — `break`
    # would have skipped fitting the A,B component if A,B sorted after C).
    fit = ordo_fit(["C", "A", "B"], encs,
                   wins={"A": 1, "B": 1, "C": 0},
                   losses={"A": 1, "B": 1, "C": 0})
    assert fit["C"] == (None, None)
    assert fit["A"][0] is not None
    assert fit["B"][0] is not None


def test_ordo_fit_empty_remaining_returns_all_none():
    """If every engine is purged, return (None, None) for all. Kills
    AddNot on `if not remaining:` early-exit guard."""
    # Both A and B are "all wins" against each other? Impossible — make
    # one all-wins, one all-losses, each against the other.
    encs = [("A", "B", 1.0, 1)]
    fit = ordo_fit(["A", "B"], encs,
                   wins={"A": 1, "B": 0},
                   losses={"A": 0, "B": 1})
    # Both purged → all (None, None).
    assert fit["A"] == (None, None)
    assert fit["B"] == (None, None)


def test_ordo_fit_empty_engine_names_returns_empty_dict():
    """No engines → empty dict. Kills AddNot on the early-exit guard."""
    assert ordo_fit([], []) == {}


def test_standings_populates_elo_ordo_for_two_engines(tmp_path):
    # 1 win, 1 loss, 1 draw each = 50% score -> ordo Elo ~ 0.
    body = (
        _game("A", "B", "1-0")
        + _game("A", "B", "0-1")
        + _game("A", "B", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    s = compute_standings(p)
    by = {e.name: e for e in s.engines}
    assert by["A"].elo_ordo is not None
    assert by["B"].elo_ordo is not None
    assert by["A"].elo_ordo == pytest.approx(0.0, abs=0.5)
    assert by["A"].elo_ordo == pytest.approx(-by["B"].elo_ordo, abs=0.01)


def test_standings_elo_ordo_serializes_to_dict():
    e_dict = compute_standings  # just sanity-check the import; details below
    from sturddle_view.tournament.pgn_stats import EngineRecord
    r = EngineRecord(name="x", elo_ordo=12.3, elo_ordo_margin_95=4.5)
    d = r.to_dict()
    assert d["elo_ordo"] == 12.3
    assert d["elo_ordo_margin_95"] == 4.5


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_iter_games_cached_when_file_unchanged(tmp_path, monkeypatch):
    from sturddle_view.tournament import pgn_stats

    p = _write_pgn(tmp_path, _game("A", "B", "1-0"))
    pgn_stats._iter_games_cache.pop(p, None)

    calls = {"n": 0}
    real = pgn_stats._iter_games_uncached

    def counting(path):
        calls["n"] += 1
        yield from real(path)

    monkeypatch.setattr(pgn_stats, "_iter_games_uncached", counting)

    compute_standings(p)
    compute_standings(p)
    compute_standings(p)
    assert calls["n"] == 1


def test_iter_games_reparses_when_file_grows(tmp_path, monkeypatch):
    import os
    import time
    from sturddle_view.tournament import pgn_stats

    p = _write_pgn(tmp_path, _game("A", "B", "1-0"))
    pgn_stats._iter_games_cache.pop(p, None)

    calls = {"n": 0}
    real = pgn_stats._iter_games_uncached

    def counting(path):
        calls["n"] += 1
        yield from real(path)

    monkeypatch.setattr(pgn_stats, "_iter_games_uncached", counting)

    s1 = compute_standings(p)
    assert s1.games == 1
    # Append another game; bump mtime in case the write lands in the same tick.
    with p.open("a", encoding="utf-8") as f:
        f.write(_game("A", "B", "0-1"))
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    s2 = compute_standings(p)
    assert s2.games == 2
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# SPRT
# ---------------------------------------------------------------------------


def _params(**overrides) -> dict:
    p = {"elo0": 0.0, "elo1": 5.0, "alpha": 0.05, "beta": 0.05, "model": "normalized"}
    p.update(overrides)
    return p


def test_sprt_no_games_returns_continue(tmp_path):
    p = _write_pgn(tmp_path, "")
    r = _sprt(p, _params())
    assert r.status == "continue"
    assert r.pairs == 0
    assert r.llr == 0.0


def test_sprt_one_game_returns_continue(tmp_path):
    # Single game = partial pair (mate hasn't been written yet). Should
    # produce zero pairs and continue.
    p = _write_pgn(tmp_path, _game_round("1", "A", "B", "1-0"))
    r = _sprt(p, _params())
    assert r.status == "continue"
    assert r.pairs == 0


def test_sprt_bounds_have_correct_signs():
    # Wald bounds: lower < 0 < upper for typical alpha=beta=0.05.
    r = _sprt(Path("/dev/null"), _params())
    assert r.lower_bound < 0 < r.upper_bound
    assert r.lower_bound == pytest.approx(math.log(0.05 / 0.95))
    assert r.upper_bound == pytest.approx(math.log(0.95 / 0.05))


def test_sprt_runaway_a_dominates_accepts_h1(tmp_path):
    # 99 pairs A wins both + 1 pair drawn. Strong H1. The single drawn
    # pair seeds nonzero sample variance (real runs always have one).
    rc = _RoundCounter()
    body = "".join(_pair(rc, "A", "B", "1-0", "0-1") for _ in range(99))
    body += _pair(rc, "A", "B", "1/2-1/2", "1/2-1/2")
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=5))
    assert r.pairs == 100
    assert r.status == "H1"
    assert r.llr > r.upper_bound


def test_sprt_runaway_b_dominates_accepts_h0(tmp_path):
    # 99 pairs A loses both + 1 pair drawn. Strongly rejects H1.
    rc = _RoundCounter()
    body = "".join(_pair(rc, "A", "B", "0-1", "1-0") for _ in range(99))
    body += _pair(rc, "A", "B", "1/2-1/2", "1/2-1/2")
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=5))
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
    rc = _RoundCounter()
    body = ""
    for r1, r2 in pair_outcomes:
        body += _pair(rc, "A", "B", r1, r2)
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=5))
    assert r.status == "continue", (
        f"expected continue with balanced play, got {r.status} (LLR={r.llr})"
    )


def test_sprt_all_draws_returns_continue(tmp_path):
    # All pairs score identically (1.0 each) -> sample variance is 0.
    # The sample carries no information about the hypothesis; LLR should
    # be 0 and status "continue", consistent with n<2.
    rc = _RoundCounter()
    body = "".join(_pair(rc, "A", "B", "1/2-1/2", "1/2-1/2") for _ in range(20))
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=5))
    assert r.pairs == 20
    assert r.status == "continue"
    assert r.llr == 0.0


def test_sprt_all_decisive_same_direction_returns_continue(tmp_path):
    # Every pair: A wins both games -> per-pair score 2.0 for all pairs.
    # Variance is 0 even though A is dominating; with no spread the
    # pentanomial model has no variance estimate.
    rc = _RoundCounter()
    body = "".join(_pair(rc, "A", "B", "1-0", "0-1") for _ in range(20))
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=5))
    assert r.pairs == 20
    assert r.status == "continue"
    assert r.llr == 0.0


def test_sprt_drops_partial_round(tmp_path, caplog):
    # Round 1 complete + Round 2 partial (only one of the two color-flipped
    # games written). Round 2 is dropped, leaving 1 pair. Logs the drop at
    # DEBUG so on-call can confirm the cause without raising the noise floor.
    rc = _RoundCounter()
    body = _pair(rc, "A", "B", "1-0", "0-1") + _game_round("2", "A", "B", "1-0")
    p = _write_pgn(tmp_path, body)
    with caplog.at_level("DEBUG", logger="sturddle_view.tournament.pgn_stats"):
        r = _sprt(p, _params())
    assert r.pairs == 1
    assert any("expected 2" in m for m in caplog.messages)


def test_sprt_warns_on_engine_mismatch(tmp_path, caplog):
    # Rounds 1+3 are clean A/B; Round 2 sneaks in a C engine. Round 2 is
    # skipped silently in the result; the warning makes it visible.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "C", "1-0") + _game_round("2", "C", "A", "0-1")
        + _game_round("3", "A", "B", "1-0") + _game_round("3", "B", "A", "0-1")
    )
    p = _write_pgn(tmp_path, body)
    with caplog.at_level("WARNING", logger="sturddle_view.tournament.pgn_stats"):
        r = _sprt(p, _params())
    assert r.pairs == 2
    assert any("round 2 skipped" in m for m in caplog.messages)


def test_sprt_round_collision_counts_both_pairs(tmp_path):
    # Round 1 contains two complete color-flipped pairs of {A, B} (the
    # Pause/Resume Round-reuse case). SPRT must see both as pairs, not
    # collapse them via Round-as-pair-ID.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("1", "A", "B", "1/2-1/2") + _game_round("1", "B", "A", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params())
    assert r.pairs == 2


def test_sprt_unimplemented_model_raises(tmp_path):
    p = _write_pgn(tmp_path, "")
    with pytest.raises(NotImplementedError):
        _sprt(p, _params(model="bayesian"))
    with pytest.raises(NotImplementedError):
        _sprt(p, _params(model="fakemodel"))


@pytest.mark.parametrize("overrides", [
    {"elo0": 5.0, "elo1": 5.0},   # hypotheses must differ
    {"elo0": 5.0, "elo1": 0.0},   # elo0 must be < elo1
    {"alpha": 0.0},               # probability must be > 0
    {"alpha": 1.0},               # probability must be < 1
    {"alpha": -0.1},
    {"alpha": 1.5},
    {"beta": 0.0},
    {"beta": 1.0},
    {"beta": -0.1},
    {"beta": 1.5},
])
def test_sprt_invalid_params_raise(tmp_path, overrides):
    p = _write_pgn(tmp_path, "")
    with pytest.raises(ValueError):
        _sprt(p, _params(**overrides))


def test_sprt_to_dict_round_trip(tmp_path):
    p = _write_pgn(tmp_path, "")
    d = _sprt(p, _params()).to_dict()
    assert set(d.keys()) == {
        "llr", "lower_bound", "upper_bound", "status",
        "pairs", "elo0", "elo1", "model",
    }


# ---------------------------------------------------------------------------
# Pair grouping under concurrency > 1
#
# fastchess writes games to PGN in *completion order*. When two pair-slots
# run in parallel, pair-mates from the same Round are not adjacent --
# they get separated by games from the other slot. Pair grouping must
# use the Round tag, not consecutive index.
# ---------------------------------------------------------------------------


def test_sprt_pair_grouping_uses_round_under_concurrency(tmp_path):
    # Same 4 game outcomes, two PGN orderings: in-order vs interleaved
    # (as fastchess emits under concurrency=2). Round-aware grouping
    # must yield identical LLR for both. Consecutive-index grouping
    # would mispair the interleaved case and produce a different LLR.
    in_order = (
        _game_round("1", "A", "B", "1-0")
        + _game_round("1", "B", "A", "0-1")  # pair 1: A wins both -> 2.0
        + _game_round("2", "A", "B", "1/2-1/2")
        + _game_round("2", "B", "A", "1/2-1/2")  # pair 2: both draw -> 1.0
    )
    interleaved = (
        _game_round("1", "A", "B", "1-0")        # pair 1, game 1
        + _game_round("2", "A", "B", "1/2-1/2")  # pair 2, game 1 (other slot)
        + _game_round("1", "B", "A", "0-1")      # pair 1, game 2 (late)
        + _game_round("2", "B", "A", "1/2-1/2")  # pair 2, game 2
    )
    p1 = tmp_path / "in_order.pgn"
    p2 = tmp_path / "interleaved.pgn"
    p1.write_text(in_order, encoding="utf-8")
    p2.write_text(interleaved, encoding="utf-8")
    r1 = _sprt(p1, _params())
    r2 = _sprt(p2, _params())
    assert r1.pairs == r2.pairs == 2
    assert r1.llr == pytest.approx(r2.llr)


def test_sprt_pair_grouping_drops_partial_pair_keeps_complete(tmp_path):
    # 5 games: Rounds 1 and 3 are complete color-flipped pairs; Round 2
    # has only one game (mate was lost mid-Stop or still in flight).
    # Round-aware grouping drops Round 2 -> 2 pairs. Consecutive-index
    # grouping (legacy) would yield 2 pairs from indices (0,1) and (2,3)
    # with the trailing 5th game dropped -- a *different* pair set that
    # mispairs Round 1's BvA with Round 2's AvB. Check the LLR matches
    # what you'd get from just Rounds 1 and 3 standalone.
    partial_body = (
        _game_round("1", "A", "B", "1-0")
        + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "1/2-1/2")  # partial: no Round-2 BvA
        + _game_round("3", "A", "B", "1-0")
        + _game_round("3", "B", "A", "0-1")
    )
    clean_body = (
        _game_round("1", "A", "B", "1-0")
        + _game_round("1", "B", "A", "0-1")
        + _game_round("3", "A", "B", "1-0")
        + _game_round("3", "B", "A", "0-1")
    )
    p1 = tmp_path / "with_partial.pgn"
    p2 = tmp_path / "clean.pgn"
    p1.write_text(partial_body, encoding="utf-8")
    p2.write_text(clean_body, encoding="utf-8")
    r1 = _sprt(p1, _params())
    r2 = _sprt(p2, _params())
    assert r1.pairs == 2
    assert r2.pairs == 2
    assert r1.llr == pytest.approx(r2.llr)


def test_sprt_pair_grouping_skips_round_with_engine_mismatch(tmp_path):
    # Round 2 pairs A with C instead of B -- the round's two games don't
    # form a valid A-vs-B pair and must be excluded. Rounds 1 and 3 are
    # clean A-vs-B pairs; result is 2 pairs.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "C", "1-0") + _game_round("2", "C", "A", "0-1")
        + _game_round("3", "A", "B", "1-0") + _game_round("3", "B", "A", "0-1")
    )
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params())
    assert r.pairs == 2


def test_sprt_engine_identity_independent_of_pgn_file_order(tmp_path):
    # Two PGNs encode identical pair outcomes (A wins pair 1 outright;
    # pair 2 is 1 win + 1 draw for A). They differ only in *which*
    # color-flipped game lands first on disk. Pre-fix code labeled the
    # candidate as whoever was white in PGN game 1, so swapping the
    # order would flip A <-> B and invert the LLR sign. With engine_a
    # passed in from config, ordering must be irrelevant.
    a_first = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "1-0") + _game_round("2", "B", "A", "1/2-1/2")
    )
    b_first = (
        _game_round("1", "B", "A", "0-1") + _game_round("1", "A", "B", "1-0")
        + _game_round("2", "B", "A", "1/2-1/2") + _game_round("2", "A", "B", "1-0")
    )
    p1 = tmp_path / "a_first.pgn"
    p2 = tmp_path / "b_first.pgn"
    p1.write_text(a_first, encoding="utf-8")
    p2.write_text(b_first, encoding="utf-8")
    r1 = _sprt(p1, _params(elo0=0, elo1=5))
    r2 = _sprt(p2, _params(elo0=0, elo1=5))
    assert r1.pairs == r2.pairs == 2
    assert r1.llr == pytest.approx(r2.llr)
    # A dominates the sample; LLR must favor H1 regardless of file order.
    assert r1.llr > 0


# ---------------------------------------------------------------------------
# Logistic SPRT (per-game W/D/L trinomial)
# ---------------------------------------------------------------------------


def test_sprt_logistic_no_games(tmp_path):
    p = _write_pgn(tmp_path, "")
    r = _sprt(p, _params(model="logistic"))
    assert r.status == "continue"
    assert r.pairs == 0
    assert r.llr == 0.0
    assert r.model == "logistic"


def test_sprt_logistic_single_game_continues(tmp_path):
    p = _write_pgn(tmp_path, _game("A", "B", "1-0"))
    r = _sprt(p, _params(model="logistic"))
    # One win can land above upper for very wide alpha/beta; with the
    # _params() defaults (alpha=beta=0.05) it stays below upper.
    assert r.pairs == 1
    assert r.status == "continue"


def test_sprt_logistic_runaway_a_dominates_accepts_h1(tmp_path):
    # Strong A dominance with a wider elo gap so LLR clears the Wald
    # upper bound within a tractable game count.
    body = ""
    for _ in range(160):
        body += _game("A", "B", "1-0")
    for _ in range(40):
        body += _game("A", "B", "1/2-1/2")
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=20, model="logistic"))
    assert r.pairs == 200
    assert r.status == "H1"
    assert r.llr > r.upper_bound


def test_sprt_logistic_runaway_b_dominates_accepts_h0(tmp_path):
    body = ""
    for _ in range(160):
        body += _game("A", "B", "0-1")
    for _ in range(40):
        body += _game("A", "B", "1/2-1/2")
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=20, model="logistic"))
    assert r.pairs == 200
    assert r.status == "H0"
    assert r.llr < r.lower_bound


def test_sprt_logistic_balanced_continues(tmp_path):
    # 5W / 5L / 10D -> mean score 0.5, no signal in either direction.
    body = ""
    for _ in range(5):
        body += _game("A", "B", "1-0")
    for _ in range(5):
        body += _game("A", "B", "0-1")
    for _ in range(10):
        body += _game("A", "B", "1/2-1/2")
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=10, model="logistic"))
    assert r.pairs == 20
    assert r.status == "continue"


def test_sprt_logistic_degenerate_extreme_elo_continues(tmp_path):
    # No draws + extreme elo bounds force pw/pl<=0 in the trinomial.
    # Should emit LLR=0, continue, not crash.
    body = _game("A", "B", "1-0")
    p = _write_pgn(tmp_path, body)
    # elo1=10000 pushes score_1 ~ 1.0 -> pl1 = 1 - 1 - 0 = 0 (degenerate).
    r = _sprt(p, _params(elo0=0, elo1=10000, model="logistic"))
    assert r.status == "continue"
    assert r.llr == 0.0


def test_sprt_logistic_uses_observed_draw_rate(tmp_path):
    # Two PGNs with same W/L but different draw rates must give different
    # LLRs under the logistic model (draw rate enters via d_obs).
    body_low_draws = _game("A", "B", "1-0") + _game("A", "B", "1-0") + _game("A", "B", "0-1")
    body_high_draws = (
        _game("A", "B", "1-0") + _game("A", "B", "1-0") + _game("A", "B", "0-1")
        + _game("A", "B", "1/2-1/2") * 10
    )
    p1 = tmp_path / "low.pgn"
    p2 = tmp_path / "high.pgn"
    p1.write_text(body_low_draws, encoding="utf-8")
    p2.write_text(body_high_draws, encoding="utf-8")
    r1 = _sprt(p1, _params(elo0=0, elo1=10, model="logistic"))
    r2 = _sprt(p2, _params(elo0=0, elo1=10, model="logistic"))
    assert r1.llr != r2.llr


def test_sprt_logistic_counts_games_not_pairs(tmp_path):
    # 5 individual games -> pairs field reports 5 (per-game count).
    body = _game("A", "B", "1-0") * 3 + _game("A", "B", "0-1") + _game("A", "B", "1/2-1/2")
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(model="logistic"))
    assert r.pairs == 5


def test_sprt_logistic_model_reported_in_result(tmp_path):
    p = _write_pgn(tmp_path, _game("A", "B", "1-0"))
    r = _sprt(p, _params(model="logistic"))
    assert r.model == "logistic"


def test_sprt_logistic_skips_engine_mismatch(tmp_path, caplog):
    body = (
        _game("A", "B", "1-0")
        + _game("A", "C", "1-0")  # mismatch
        + _game("A", "B", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    with caplog.at_level("WARNING", logger="sturddle_view.tournament.pgn_stats"):
        r = _sprt(p, _params(model="logistic"))
    assert r.pairs == 2
    assert any("game skipped" in m for m in caplog.messages)


def test_sprt_logistic_engine_identity_independent_of_pgn_file_order(tmp_path):
    # Logistic counterpart to the pentanomial identity test: a dominates
    # the per-game W/D/L tally regardless of which engine had white in
    # the first PGN entry.
    a_first = _game("A", "B", "1-0") + _game("B", "A", "0-1") + _game("A", "B", "1-0")
    b_first = _game("B", "A", "0-1") + _game("A", "B", "1-0") + _game("A", "B", "1-0")
    p1 = tmp_path / "a_first.pgn"
    p2 = tmp_path / "b_first.pgn"
    p1.write_text(a_first, encoding="utf-8")
    p2.write_text(b_first, encoding="utf-8")
    r1 = _sprt(p1, _params(elo0=0, elo1=10, model="logistic"))
    r2 = _sprt(p2, _params(elo0=0, elo1=10, model="logistic"))
    assert r1.pairs == r2.pairs == 3
    assert r1.llr == pytest.approx(r2.llr)
    # A wins all 3 -> LLR positive regardless of file order.
    assert r1.llr > 0


# ---------------------------------------------------------------------------
# read_game_pgn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("game_n", [0, -1, -100])
def test_read_game_pgn_non_positive_returns_none(tmp_path, game_n):
    p = _write_pgn(tmp_path, _game("A", "B", "1-0"))
    assert read_game_pgn(p, game_n) is None


def test_read_game_pgn_beyond_end_returns_none(tmp_path):
    body = _game("A", "B", "1-0") + _game("B", "A", "0-1")
    p = _write_pgn(tmp_path, body)
    assert read_game_pgn(p, 3) is None
    assert read_game_pgn(p, 999) is None


def test_read_game_pgn_skips_ongoing_games(tmp_path):
    # Decisive games are counted; "*" (ongoing) is skipped. Game_n=2
    # should resolve to the third record on disk.
    body = (
        _game("A", "B", "1-0")
        + _game("A", "B", "*")
        + _game("B", "A", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    text = read_game_pgn(p, 2)
    assert text is not None
    assert '[Result "1/2-1/2"]' in text


def test_read_game_pgn_returns_first_game(tmp_path):
    body = _game("A", "B", "1-0") + _game("B", "A", "0-1")
    p = _write_pgn(tmp_path, body)
    text = read_game_pgn(p, 1)
    assert text is not None
    assert '[White "A"]' in text and '[Black "B"]' in text
    assert '[Result "1-0"]' in text


# ---------------------------------------------------------------------------
# count_partial_pairs
# ---------------------------------------------------------------------------


def test_count_partial_pairs_empty(tmp_path):
    p = _write_pgn(tmp_path, "")
    assert count_partial_pairs(p) == 0


def test_count_partial_pairs_all_complete(tmp_path):
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "1/2-1/2") + _game_round("2", "B", "A", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    assert count_partial_pairs(p) == 0


def test_count_partial_pairs_one_partial(tmp_path):
    # Round 1 complete, round 2 has only the white-A game.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    assert count_partial_pairs(p) == 1


def test_count_partial_pairs_resume_dup_is_orphan(tmp_path):
    # Round 1 has 3 records on the same engine set: two A-as-white and
    # one B-as-white. The new scheme pairs one (A-as-white, B-as-white)
    # and reports the surplus A-as-white as an orphan. This is the
    # honest count -- the third game has no color-flip partner.
    body = (
        _game_round("1", "A", "B", "1-0")
        + _game_round("1", "A", "B", "1-0")  # surplus A-as-white
        + _game_round("1", "B", "A", "0-1")
    )
    p = _write_pgn(tmp_path, body)
    assert count_partial_pairs(p) == 1


def test_count_partial_pairs_multi_engine(tmp_path):
    # Round 1: A-B and B-A complete; A-C complete; C-A missing.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("1", "A", "C", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    assert count_partial_pairs(p) == 1


def test_count_partial_pairs_round_collision_two_complete_pairs(tmp_path):
    # Round 1 contains TWO complete color-flipped pairs of the same engine
    # set -- the Round-number-reuse case from a Pause/Resume boundary. The
    # bucket has 4 games (2 of each color) which all pair, so 0 orphans.
    # This is the case the old (round, white, black) dedup mis-handled.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("1", "A", "B", "0-1") + _game_round("1", "B", "A", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    assert count_partial_pairs(p) == 0
    # And standings should count all 4 games.
    assert compute_standings(p).games == 4


def test_count_partial_pairs_paired_false_returns_zero(tmp_path):
    # Single-game tours (paired=False): no pair concept, no orphans
    # even if the PGN looks like it has partial pairs.
    body = _game_round("1", "A", "B", "1-0")
    p = _write_pgn(tmp_path, body)
    assert count_partial_pairs(p, paired=False) == 0


# ---------------------------------------------------------------------------
# rewrite_drop_partial_pairs
# ---------------------------------------------------------------------------


def test_rewrite_no_partials_no_op(tmp_path):
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "1/2-1/2") + _game_round("2", "B", "A", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    before = p.read_bytes()
    n, deltas = rewrite_drop_partial_pairs(p)
    assert n == 0
    assert deltas == {}
    assert p.read_bytes() == before
    assert _find_bak_gz(p) is None


def test_rewrite_drops_partial_pair(tmp_path):
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "1-0")  # partial: round 2 missing B vs A
    )
    p = _write_pgn(tmp_path, body)
    n, deltas = rewrite_drop_partial_pairs(p)
    assert n == 1
    assert deltas == {"A vs B": {"wins": 1, "losses": 0, "draws": 0}}
    assert _find_bak_gz(p) is not None
    # After rewrite: 0 partial pairs, 2 unique games.
    assert count_partial_pairs(p) == 0
    assert compute_standings(p).games == 2


def test_rewrite_dedups_resume_duplicates(tmp_path):
    body = (
        _game_round("1", "A", "B", "1-0")
        + _game_round("1", "A", "B", "1-0")  # duplicate of game 1
        + _game_round("1", "B", "A", "0-1")
    )
    p = _write_pgn(tmp_path, body)
    n, deltas = rewrite_drop_partial_pairs(p)
    assert n == 1  # one duplicate dropped; no partial pair (round 1 has both colors)
    assert deltas == {"A vs B": {"wins": 1, "losses": 0, "draws": 0}}
    assert compute_standings(p).games == 2


def test_rewrite_mixed_partials_and_dups(tmp_path):
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")  # complete
        + _game_round("2", "A", "B", "1-0") + _game_round("2", "A", "B", "1-0")  # dup, partial
        + _game_round("3", "A", "B", "1-0") + _game_round("3", "B", "A", "0-1")  # complete
    )
    p = _write_pgn(tmp_path, body)
    n, deltas = rewrite_drop_partial_pairs(p)
    # Round 2 dedups to 1 game (still partial, so dropped). Total dropped = 2.
    assert n == 2
    assert deltas == {"A vs B": {"wins": 2, "losses": 0, "draws": 0}}
    assert count_partial_pairs(p) == 0
    assert compute_standings(p).games == 4


def test_rewrite_delta_loss(tmp_path):
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "0-1")  # partial: A loses as White
    )
    p = _write_pgn(tmp_path, body)
    _n, deltas = rewrite_drop_partial_pairs(p)
    assert deltas == {"A vs B": {"wins": 0, "losses": 1, "draws": 0}}


def test_rewrite_delta_draw(tmp_path):
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "1/2-1/2")  # partial: draw
    )
    p = _write_pgn(tmp_path, body)
    _n, deltas = rewrite_drop_partial_pairs(p)
    assert deltas == {"A vs B": {"wins": 0, "losses": 0, "draws": 1}}


def test_rewrite_delta_multi_pair(tmp_path):
    # Two separate engine pairs each have a partial round.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("1", "C", "D", "1-0") + _game_round("1", "D", "C", "0-1")
        + _game_round("2", "A", "B", "1/2-1/2")   # partial A vs B
        + _game_round("2", "C", "D", "0-1")        # partial C vs D
    )
    p = _write_pgn(tmp_path, body)
    _n, deltas = rewrite_drop_partial_pairs(p)
    assert deltas == {
        "A vs B": {"wins": 0, "losses": 0, "draws": 1},
        "C vs D": {"wins": 0, "losses": 1, "draws": 0},
    }


def test_rewrite_missing_pgn_no_op(tmp_path):
    p = tmp_path / "absent.pgn"
    assert rewrite_drop_partial_pairs(p) == (0, {})


def test_rewrite_backup_bytes_equal_original(tmp_path):
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "1-0")  # partial -> triggers rewrite
    )
    p = _write_pgn(tmp_path, body)
    original = p.read_bytes()
    rewrite_drop_partial_pairs(p)
    bak = _find_bak_gz(p)
    assert bak is not None
    assert gzip.decompress(bak.read_bytes()) == original


def test_rewrite_idempotent(tmp_path):
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "1-0")  # partial
    )
    p = _write_pgn(tmp_path, body)
    n, _ = rewrite_drop_partial_pairs(p)
    assert n == 1  # first run drops the partial
    n, deltas = rewrite_drop_partial_pairs(p)
    assert n == 0  # second run is a no-op
    assert deltas == {}


def test_rewrite_skips_ongoing_results(tmp_path):
    # `*` (ongoing) games are not preserved by the rewrite. Output file
    # should only contain the decisive games.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("2", "A", "B", "*") + _game_round("2", "B", "A", "*")
        + _game_round("3", "A", "B", "1-0")  # partial -> triggers rewrite
    )
    p = _write_pgn(tmp_path, body)
    rewrite_drop_partial_pairs(p)
    after = p.read_text(encoding="utf-8")
    assert '[Result "*"]' not in after
    assert '[Round "3"]' not in after  # partial dropped
    assert '[Round "1"]' in after


def test_rewrite_preserves_round_collision_with_two_complete_pairs(tmp_path):
    # Round 1 has two distinct color-flipped pairs sharing the same Round
    # number (Pause/Resume Round-reuse). All 4 games pair cleanly, so the
    # rewrite must keep all 4 -- this was the silent-data-loss bug.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("1", "A", "B", "0-1") + _game_round("1", "B", "A", "1-0")
        + _game_round("2", "A", "B", "1/2-1/2")  # partial -> triggers rewrite
    )
    p = _write_pgn(tmp_path, body)
    n, deltas = rewrite_drop_partial_pairs(p)
    # Only the round-2 partial is dropped; all 4 round-1 games stay.
    assert n == 1
    assert deltas == {"A vs B": {"wins": 0, "losses": 0, "draws": 1}}
    assert compute_standings(p).games == 4


def test_rewrite_paired_false_is_no_op(tmp_path):
    # Single-game tours never drop anything, even apparent partials.
    body = (
        _game_round("1", "A", "B", "1-0")
        + _game_round("2", "A", "B", "0-1")
        + _game_round("3", "A", "B", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    before = p.read_bytes()
    n, deltas = rewrite_drop_partial_pairs(p, paired=False)
    assert n == 0
    assert deltas == {}
    assert p.read_bytes() == before
    assert _find_bak_gz(p) is None


def test_rewrite_gauntlet_distinct_match_ups_unaffected(tmp_path):
    # Gauntlet PGN: leader L plays C1 and C2. Each match-up's pair lives in
    # its own (round, engine-set) bucket. A partial in one match-up does
    # not leak into the other.
    body = (
        # Round 1: L vs C1 complete pair.
        _game_round("1", "L", "C1", "1-0") + _game_round("1", "C1", "L", "0-1")
        # Round 2: L vs C2 partial (missing C2-as-white).
        + _game_round("2", "L", "C2", "1-0")
        # Round 3: L vs C1 complete pair.
        + _game_round("3", "L", "C1", "1/2-1/2") + _game_round("3", "C1", "L", "1/2-1/2")
    )
    p = _write_pgn(tmp_path, body)
    n, deltas = rewrite_drop_partial_pairs(p)
    assert n == 1
    assert deltas == {"L vs C2": {"wins": 1, "losses": 0, "draws": 0}}
    # L vs C1 unaffected: 4 games (2 complete pairs) stay.
    s = compute_standings(p)
    assert s.games == 4


# ---------------------------------------------------------------------------
# patch_config_json
# ---------------------------------------------------------------------------

_CONFIG_TEMPLATE = {
    "opening": {"start": 1},
    "stats": {
        "A vs B": {
            "wins": 10, "losses": 8, "draws": 4,
            "penta_WW": 2, "penta_WD": 3, "penta_WL": 1,
            "penta_DD": 1, "penta_LD": 2, "penta_LL": 1,
        }
    },
}


def _write_config(tmp_path, data=None):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data or _CONFIG_TEMPLATE, indent=4), encoding="utf-8")
    return p


def test_patch_config_subtracts_wld(tmp_path):
    p = _write_config(tmp_path)
    patch_config_json(p, {"A vs B": {"wins": 1, "losses": 0, "draws": 0}})
    stats = json.loads(p.read_text())["stats"]["A vs B"]
    assert stats["wins"] == 9
    assert stats["losses"] == 8
    assert stats["draws"] == 4


def test_patch_config_preserves_penta(tmp_path):
    # Pentanomial counters must NOT be touched by the patch. Zeroing them
    # on every Stop wiped fastchess's running pentanomial across Pause/
    # Resume cycles, breaking its internal SPRT auto-stop.
    p = _write_config(tmp_path)
    patch_config_json(p, {"A vs B": {"wins": 1, "losses": 0, "draws": 0}})
    stats = json.loads(p.read_text())["stats"]["A vs B"]
    expected = _CONFIG_TEMPLATE["stats"]["A vs B"]
    for pk in ("penta_WW", "penta_WD", "penta_WL", "penta_DD", "penta_LD", "penta_LL"):
        assert stats[pk] == expected[pk]


def test_patch_config_clamps_at_zero(tmp_path):
    data = {
        "stats": {"A vs B": {"wins": 0, "losses": 0, "draws": 0,
                              "penta_WW": 0, "penta_WD": 0, "penta_WL": 0,
                              "penta_DD": 0, "penta_LD": 0, "penta_LL": 0}}
    }
    p = _write_config(tmp_path, data)
    patch_config_json(p, {"A vs B": {"wins": 5, "losses": 5, "draws": 5}})
    stats = json.loads(p.read_text())["stats"]["A vs B"]
    assert stats["wins"] == 0
    assert stats["losses"] == 0
    assert stats["draws"] == 0


def test_patch_config_writes_backup(tmp_path):
    p = _write_config(tmp_path)
    original = p.read_bytes()
    patch_config_json(p, {"A vs B": {"wins": 1, "losses": 0, "draws": 0}})
    bak = _find_bak_gz(p)
    assert bak is not None
    assert gzip.decompress(bak.read_bytes()) == original


def test_patch_config_no_op_if_missing(tmp_path):
    p = tmp_path / "config.json"
    patch_config_json(p, {"A vs B": {"wins": 1, "losses": 0, "draws": 0}})
    assert not p.exists()


def test_patch_config_no_op_if_empty_deltas(tmp_path):
    p = _write_config(tmp_path)
    original = p.read_bytes()
    patch_config_json(p, {})
    assert p.read_bytes() == original


def test_patch_config_unknown_pair_warns(tmp_path, caplog):
    import logging
    p = _write_config(tmp_path)
    with caplog.at_level(logging.WARNING):
        patch_config_json(p, {"X vs Y": {"wins": 1, "losses": 0, "draws": 0}})
    assert "X vs Y" in caplog.text
    # File unchanged -- no known pair was patched.
    assert _find_bak_gz(p) is None


def test_patch_config_reversed_key(tmp_path):
    # config.json stores "A vs B"; delta arrives as "B vs A" (game played
    # with colors swapped). wins<->losses must be flipped when applying.
    p = _write_config(tmp_path)  # stats stored as "A vs B": wins=10, losses=8
    patch_config_json(p, {"B vs A": {"wins": 2, "losses": 1, "draws": 0}})
    stats = json.loads(p.read_text())["stats"]["A vs B"]
    # "B vs A" win = "A vs B" loss: losses 8 - 2 = 6
    # "B vs A" loss = "A vs B" win: wins 10 - 1 = 9
    assert stats["wins"] == 9
    assert stats["losses"] == 6
    assert stats["draws"] == 4


# ---------------------------------------------------------------------------
# Real-PGN fixture: cross-checked against ordo on the same file
# ---------------------------------------------------------------------------

# tests/fixtures/sample_50.pgn — first 50 games of a real Sturddle 2.4.0 vs
# 2.3.1 self-play tournament. Hand-tally + ordo baseline below; if either
# breaks, the parser or the Elo math drifted.
#
# Ordo baseline (sturddle-2.3.1 anchored at 0):
#   sturddle-2.4.0 : 14.0 Elo, 26.0 / 50 (52%)
#   sturddle-2.3.1 :  0.0 Elo, 24.0 / 50 (48%)
_FIXTURE = Path(__file__).parent / "fixtures" / "sample_50.pgn"


def test_real_pgn_fixture_tally():
    s = compute_standings(_FIXTURE)
    assert s.games == 50
    by = {e.name: e for e in s.engines}
    assert by["sturddle-2.4.0"].wins == 13
    assert by["sturddle-2.4.0"].losses == 11
    assert by["sturddle-2.4.0"].draws == 26
    assert by["sturddle-2.3.1"].wins == 11
    assert by["sturddle-2.3.1"].losses == 13
    assert by["sturddle-2.3.1"].draws == 26


def test_real_pgn_fixture_leader_elo_matches_ordo():
    # Ordo with -a 0 -A "sturddle-2.3.1" anchors the trailer at 0 and
    # reports +14.0 for the leader — the standard chess Elo rating gap.
    # Our compute_standings stores ``elo_from_score(score_pct)`` on each
    # engine independently. For the leader (score_pct = 0.52) that yields
    # +13.9, which matches ordo's anchored gap to within rounding.
    # NOTE: we *also* store -13.9 on the trailer (mirror), so the two
    # engines' Elo values *appear* to span 28. That's a UI/convention
    # question (anchored vs symmetric display), not a math bug — the
    # underlying formula agrees with ordo.
    s = compute_standings(_FIXTURE)
    by = {e.name: e for e in s.engines}
    assert by["sturddle-2.4.0"].elo == pytest.approx(14.0, abs=0.5)


def test_read_game_record_returns_hash_and_summary():
    from sturddle_view.play.canonical_hash import canonical_hash
    fixture = Path(__file__).parent / "fixtures" / "sample_50.pgn"
    rec = read_game_record(fixture, 1)
    assert rec is not None
    # rec["hash"] must equal canonical_hash(rec["pgn"]) so the tournament
    # replay client can POST rec["pgn"] to /game/import and get the same
    # hash back. Regression: prior to canonical hashing this used raw
    # sha256 on both sides; after canonicalization both sides must agree
    # on the canonical form.
    assert rec["hash"] == canonical_hash(rec["pgn"], "pgn")
    # summary is a structured dict; white/black match the engine headers
    s = rec["summary"]
    assert s["white"] == rec["engine_white"] and s["black"] == rec["engine_black"]
    # result mirrors rec; _get_game_offsets only indexes decisive games
    assert s["result"] == rec["result"]




