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
    patch_config_json,
    read_game_pgn,
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


def test_count_partial_pairs_resume_dups_dont_count(tmp_path):
    # Round 1 has 3 raw records (resume wrote game1 twice). Dedup leaves
    # 2 -- one per color. Should NOT count as partial.
    body = (
        _game_round("1", "A", "B", "1-0")
        + _game_round("1", "A", "B", "1-0")  # duplicate
        + _game_round("1", "B", "A", "0-1")
    )
    p = _write_pgn(tmp_path, body)
    assert count_partial_pairs(p) == 0


def test_count_partial_pairs_multi_engine(tmp_path):
    # Round 1: A-B and B-A complete; A-C complete; C-A missing.
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("1", "A", "C", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    assert count_partial_pairs(p) == 1


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


def test_patch_config_zeros_penta(tmp_path):
    p = _write_config(tmp_path)
    patch_config_json(p, {"A vs B": {"wins": 1, "losses": 0, "draws": 0}})
    stats = json.loads(p.read_text())["stats"]["A vs B"]
    for pk in ("penta_WW", "penta_WD", "penta_WL", "penta_DD", "penta_LD", "penta_LL"):
        assert stats[pk] == 0


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
