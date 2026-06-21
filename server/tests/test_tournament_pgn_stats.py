"""pgn_stats -- PGN-driven standings, Elo, SPRT, and games list.

Fixture PGNs are hand-crafted strings inside the tests; no external
PGN files. Each test writes to a tmp file because the public API takes
a Path."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

import json

from sturddle_view.tournament.pgn_stats import (
    compute_games_list,
    compute_sprt,
    compute_standings,
    elo_from_score,
    elo_margin_from_wld,
    games_played_from_config,
    ordo_fit,
    read_game_pgn,
    read_game_record,
)


_FIXTURE_RESUME_CONFIG = (
    Path(__file__).parent / "fixtures" / "tournament_resume" / "config.json"
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
    # (Round, White, Black). Round is not a reliable pair ID, so
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


def test_gauntlet_first_contact_white_win_pins_wld_init(tmp_path):
    """First game between leader (A) and challenger (B) is B-as-white
    winning. This makes line `setdefault(B, {}).setdefault(A, [0,0,0])`
    in the WHITE_WIN branch the FIRST writer of wld[B][A]. Add 2 more
    games where A wins as white so the final score is non-degenerate
    (1W 2L for B), pinning both the init AND the increment count via
    the resulting Elo."""
    body = (
        _game("B", "A", "1-0")   # B-as-white wins (FIRST B-vs-A contact, white-win-branch init lives)
        + _game("A", "B", "1-0")  # A wins
        + _game("A", "B", "1-0")  # A wins
        + _game("A", "C", "1-0")  # leader pad
        + _game("C", "A", "0-1")  # leader pad
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p, tournament_type="gauntlet").to_dict()
    by = {e["name"]: e for e in d["engines"]}
    # B vs A: 1W 2L 0D -> score 1/3 -> elo ~= -191.
    import math
    expected_elo = -400 * math.log10((1 - 1/3) / (1/3))
    assert by["B"]["elo"] == pytest.approx(expected_elo, abs=0.01)
    from sturddle_view.tournament.pgn_stats import elo_margin_from_wld
    assert by["B"]["elo_margin_95"] == pytest.approx(
        elo_margin_from_wld(1, 2, 0), abs=0.01,
    )


def test_gauntlet_first_contact_black_win_pins_wld_init(tmp_path):
    """First game between leader (A) and challenger (B) is B-as-black
    winning. This makes line `setdefault(B, {}).setdefault(A, [0,0,0])`
    in the BLACK_WIN branch the FIRST writer of wld[B][A]. Pins the
    `[0, 0, 0]` initializer slots in that line against NumberReplacer
    mutations."""
    body = (
        _game("A", "B", "0-1")   # B-as-black wins (FIRST B-vs-A contact)
        + _game("B", "A", "0-1")  # A wins as black
        + _game("B", "A", "0-1")  # A wins as black
        + _game("A", "C", "1-0")  # leader pad
        + _game("C", "A", "0-1")  # leader pad
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p, tournament_type="gauntlet").to_dict()
    by = {e["name"]: e for e in d["engines"]}
    # B vs A: 1W 2L 0D -> score 1/3 -> exact Elo.
    import math
    expected_elo = -400 * math.log10((1 - 1/3) / (1/3))
    assert by["B"]["elo"] == pytest.approx(expected_elo, abs=0.01)
    from sturddle_view.tournament.pgn_stats import elo_margin_from_wld
    assert by["B"]["elo_margin_95"] == pytest.approx(
        elo_margin_from_wld(1, 2, 0), abs=0.01,
    )


def test_gauntlet_first_contact_draw_pins_wld_init(tmp_path):
    """First game between leader and challenger is a draw. Pins the
    `[0, 0, 0]` init in the DRAW branch (line writing wld[white][black]
    and wld[black][white]) against NumberReplacer mutations."""
    body = (
        _game("A", "B", "1/2-1/2")  # draw (FIRST B-vs-A contact, draw-branch init lives)
        + _game("A", "B", "1-0")    # A wins
        + _game("B", "A", "0-1")    # A wins
        + _game("A", "C", "1-0")    # leader pad
        + _game("C", "A", "0-1")    # leader pad
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p, tournament_type="gauntlet").to_dict()
    by = {e["name"]: e for e in d["engines"]}
    # B vs A: 0W 2L 1D -> score 0.5/3 = 1/6 -> elo ~= -279.
    import math
    expected_elo = -400 * math.log10((1 - 1/6) / (1/6))
    assert by["B"]["elo"] == pytest.approx(expected_elo, abs=0.01)
    from sturddle_view.tournament.pgn_stats import elo_margin_from_wld
    assert by["B"]["elo_margin_95"] == pytest.approx(
        elo_margin_from_wld(0, 2, 1), abs=0.01,
    )


def test_three_engine_roundrobin_does_not_compute_per_engine_elo(tmp_path):
    """3-engine roundrobin with non-degenerate scores must NOT produce
    per-engine head-to-head Elo. Kills `and` -> `or` mutation on the
    gauntlet branch guard `len(engines) >= 3 and tournament_type ==
    'gauntlet'` (which would let the gauntlet branch fire for any 3+
    engine tour regardless of tournament_type)."""
    # Asymmetric scores so the gauntlet branch WOULD produce non-None
    # Elo if it mistakenly fired. The existing 3-engine test has A
    # sweeping both opponents -> elo=None even from gauntlet branch.
    body = (
        _game("A", "B", "1-0") + _game("B", "A", "0-1")
        + _game("A", "C", "1/2-1/2") + _game("C", "A", "1/2-1/2")
        + _game("B", "C", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    d = compute_standings(p, tournament_type="roundrobin").to_dict()
    for e in d["engines"]:
        # Per-engine Elo only makes sense head-to-head; suppress in RR.
        assert e["elo"] is None, f"{e['name']} got elo={e['elo']}"
        assert e["elo_margin_95"] is None


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
    on the head-to-head guard and `>= 2`→`>= 1` on the ordo guard."""
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


def test_elo_margin_from_wld_exactly_two_games_returns_finite():
    """At n=2 the CI is defined (n < 2 guard is strict-less-than). Kills
    `n < 2` -> `n <= 2` / `n < 3` mutations on the under-games guard
    (which would force None at the n=2 boundary)."""
    # 1W 1L is 50% score, n=2 -> margin defined.
    result = elo_margin_from_wld(1, 1, 0)
    assert result is not None
    assert result > 0


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

from sturddle_view.tournament.pgn_stats import _form_pairs, _ordo_fit_margins, _ordo_iterative_fit  # noqa: E402


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
    # Tight tolerance pins the var_last initializer (NumberReplacer 0.0 ->
    # 1.0/-1.0 produces drift ~0.006 -- inside abs=0.01, outside abs=1e-4).
    assert result["A"] == pytest.approx(198.6938, abs=1e-4)
    assert result["B"] == pytest.approx(188.2517, abs=1e-4)
    assert result["C"] == pytest.approx(338.2265, abs=1e-4)


def test_ordo_fit_margins_four_engines_exact():
    """4-engine case: exercises the n=4 Gauss-Jordan path with size=3.
    Pins:
      - `size = n - 1` against `n ^ 1` (n=4: 3 vs 5 -> indexing crash)
      - `range(2 * size)` against `range(2 + size)` (size=3: 6 vs 5
         elements per augmented row -> incomplete elimination, wrong inverse)
    Smaller engines (n<=3) don't distinguish those mutations because
    `2*2 == 2+2` and `3^1 == 3-1`."""
    engines = ["A", "B", "C", "D"]
    enc = [
        ("A", "B", 6.0, 10), ("A", "C", 5.0, 10), ("A", "D", 7.0, 10),
        ("B", "C", 4.0, 10), ("B", "D", 5.0, 10), ("C", "D", 6.0, 10),
    ]
    ratings = {"A": 50.0, "B": 0.0, "C": -25.0, "D": -25.0}
    result = _ordo_fit_margins(engines, enc, ratings)
    assert result["A"] == pytest.approx(156.0488, abs=1e-4)
    assert result["B"] == pytest.approx(154.4801, abs=1e-4)
    assert result["C"] == pytest.approx(154.5674, abs=1e-4)
    assert result["D"] == pytest.approx(379.4239, abs=1e-4)


def test_ordo_fit_margins_low_variance_returns_finite_margin():
    """Very high sample sizes drive Fisher info up and the inverse-matrix
    diagonal `v` below 1.0. Margin must still be finite. Kills
    NumberReplacer `v >= 0` -> `v >= 1` on the variance positivity guard
    (which would force margin=None when 0 <= v < 1)."""
    engines = ["A", "B", "C"]
    enc = [
        ("A", "B", 5_000_000.0, 10_000_000),
        ("A", "C", 5_000_000.0, 10_000_000),
        ("B", "C", 5_000_000.0, 10_000_000),
    ]
    ratings = {"A": 0.0, "B": 0.0, "C": 0.0}
    result = _ordo_fit_margins(engines, enc, ratings)
    # v_A ~ 0.008 (well below 1); under `v >= 1` mutation margin -> None.
    assert result["A"] is not None
    assert result["A"] < 1.96  # confirms v < 1


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


# ---------------------------------------------------------------------------
# _form_pairs — exercise directly (only used via callers in prod)
# ---------------------------------------------------------------------------


def test_form_pairs_default_paired_true():
    """Default `paired=True` keyword applies when omitted. Kills
    `True`→`False` mutation on the default value (which would force
    callers using the default to receive ([], [])."""
    keyed = [
        ("1", "A", "B", "1-0"),
        ("1", "B", "A", "0-1"),
    ]
    pairs, orphans = _form_pairs(keyed)  # no paired= kwarg
    assert pairs == [(0, 1)]
    assert orphans == []


def test_form_pairs_unpaired_returns_empty_for_nonempty_input():
    """`paired=False` with non-empty keyed must short-circuit to ([], []).
    Kills `or`→`and` on the early-return guard (which would let processing
    continue and emit pairs/orphans for a non-paired tour)."""
    keyed = [
        ("1", "A", "B", "1-0"),
        ("1", "B", "A", "0-1"),
    ]
    assert _form_pairs(keyed, paired=False) == ([], [])


def test_form_pairs_empty_round_tag_becomes_orphan():
    """An entry with empty Round tag is orphaned (not bucketed). Kills
    `or`→`and` on the round-tag guard."""
    keyed = [("", "A", "B", "1-0")]
    pairs, orphans = _form_pairs(keyed, paired=True)
    assert pairs == []
    assert orphans == [0]


def test_form_pairs_question_mark_round_becomes_orphan():
    """Round=='?' is treated as no-round and orphaned. Kills `==`→`>`/`is`
    mutations on the `round_tag == '?'` comparison."""
    keyed = [("?", "A", "B", "1-0")]
    pairs, orphans = _form_pairs(keyed, paired=True)
    assert pairs == []
    assert orphans == [0]


def test_form_pairs_two_no_round_entries_do_not_pair():
    """Two color-flipped entries with empty round tags must both be
    orphaned -- the no-round guard prevents them from being bucketed
    together. Kills `or` -> `and` mutation on the no-round guard
    (which would funnel everything into buckets and pair these two)."""
    keyed = [("", "A", "B", "1-0"), ("", "B", "A", "0-1")]
    pairs, orphans = _form_pairs(keyed, paired=True)
    assert pairs == []
    assert sorted(orphans) == [0, 1]


def test_form_pairs_two_question_mark_entries_do_not_pair():
    """Same as the empty-round case but with `'?'` as the round tag.
    Pins both branches of `not round_tag or round_tag == '?'` against
    the `and` mutation."""
    keyed = [("?", "A", "B", "1-0"), ("?", "B", "A", "0-1")]
    pairs, orphans = _form_pairs(keyed, paired=True)
    assert pairs == []
    assert sorted(orphans) == [0, 1]


def test_form_pairs_no_round_continue_processes_subsequent_entries():
    """An entry with no round must `continue` (not `break`): later
    entries with real rounds must still be bucketed and paired. Kills
    ReplaceContinueWithBreak in the no-round branch."""
    keyed = [
        ("", "A", "B", "1-0"),                # no round → orphan
        ("1", "A", "B", "1-0"),               # paired with next
        ("1", "B", "A", "0-1"),
    ]
    pairs, orphans = _form_pairs(keyed, paired=True)
    assert pairs == [(1, 2)]
    assert sorted(orphans) == [0]


def test_form_pairs_singleton_bucket_becomes_orphan():
    """A bucket with one game (no color-flip partner) is orphaned."""
    keyed = [("1", "A", "B", "1-0")]
    pairs, orphans = _form_pairs(keyed, paired=True)
    assert pairs == []
    assert orphans == [0]


def test_form_pairs_pair_order_uses_lower_file_index_first():
    """When the i-th `A-white` game is at a later file index than the
    i-th `B-white` game, the pair tuple lists the *lower* index first.
    Kills `<` → other comparison mutations on the tuple-order normalizer."""
    # B-white at index 0, A-white at index 1 → pair should be (0, 1).
    keyed = [
        ("1", "B", "A", "0-1"),  # B as white
        ("1", "A", "B", "1-0"),  # A as white
    ]
    pairs, orphans = _form_pairs(keyed, paired=True)
    assert pairs == [(0, 1)]
    assert orphans == []


def test_form_pairs_pair_order_swaps_when_side_a_index_greater():
    """When the k-th A-white game comes AFTER the k-th B-white game in
    file order, the pair tuple must still list the lower index first.
    Pins the `if ia < ib` swap branch (which is unreachable when both
    side_a and side_b are in strict ascending file order)."""
    keyed = [
        ("1", "A", "B", "1-0"),  # 0: A-white
        ("1", "B", "A", "0-1"),  # 1: B-white
        ("1", "B", "A", "0-1"),  # 2: B-white
        ("1", "A", "B", "1-0"),  # 3: A-white
    ]
    pairs, orphans = _form_pairs(keyed, paired=True)
    # k=0: side_a[0]=0 paired with side_b[0]=1 -> (0, 1) (no swap).
    # k=1: side_a[1]=3 paired with side_b[1]=2 -> 3 > 2 -> swap -> (2, 3).
    assert sorted(pairs) == [(0, 1), (2, 3)]
    assert orphans == []


def test_form_pairs_partition_by_first_white():
    """Bucket entries are split by which engine is white (per the first
    entry's white). Asymmetric counts → surplus side becomes orphans.
    Kills `==`→`!=`/Is/IsNot mutations on the partitioning predicate
    and NumberReplacers on the `keyed[idx][1]` white-name lookup."""
    # 3 A-as-white + 1 B-as-white in same bucket → 1 pair + 2 orphans (extra A).
    keyed = [
        ("1", "A", "B", "1-0"),  # A white
        ("1", "A", "B", "1-0"),  # A white
        ("1", "A", "B", "1-0"),  # A white
        ("1", "B", "A", "0-1"),  # B white
    ]
    pairs, orphans = _form_pairs(keyed, paired=True)
    assert len(pairs) == 1
    # The single pair pairs one A-white with the one B-white.
    assert pairs[0] == (0, 3)
    # The two surplus A-whites are orphans.
    assert sorted(orphans) == [1, 2]


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


def test_ordo_fit_purges_all_losses_engine_in_multi_component_pool():
    """All-losses engine must be purged even when the remaining engines
    form a non-degenerate component after the all-wins engine is also
    removed. Kills `losses > 0` -> `== 0`/`< 0` mutations on the
    second-leg purge predicate (which would fail to fire for an
    all-losses engine, leaving it in the fit with a real rating)."""
    # A: 2W 0L (all wins) -> purged via first leg
    # C: 0W 2L (all losses) -> purged via second leg
    # B, D: balanced -> form a real component
    encs = [
        ("A", "B", 1.0, 1),    # A beats B
        ("A", "C", 1.0, 1),    # A beats C
        ("B", "D", 1.0, 1),    # B beats D
        ("D", "C", 1.0, 1),    # D beats C
    ]
    fit = ordo_fit(["A", "B", "C", "D"], encs,
                   wins={"A": 2, "B": 1, "C": 0, "D": 1},
                   losses={"A": 0, "B": 1, "C": 2, "D": 1})
    # A and C are purged; B and D fit each other.
    assert fit["A"] == (None, None)
    assert fit["C"] == (None, None)
    # If the all-losses-leg mutation slipped, C would have a real Elo
    # because removing A still leaves C connected via D.
    assert fit["B"][0] is not None
    assert fit["D"][0] is not None


def test_ordo_fit_purges_engine_missing_from_wins_dict_only_when_paired_with_losses():
    """An engine absent from the `wins` dict (defaults to 0) but PRESENT
    in `losses` with a positive count must be purged via the
    "all losses" leg. Kills NumberReplacer mutations on the `.get(n, 0)`
    defaults (specifically `0` -> `1` on wins.get and `0` -> `-1` on
    losses.get) which would change which dict-default-bound engines get
    purged."""
    # Z is absent from wins (default 0) but has losses=2. Original
    # purge predicate: (wins=0 > 0)=False; (losses=2 > 0 and wins=0 == 0)=True
    # -> Z purged.
    encs = [
        ("A", "B", 1.0, 1), ("B", "A", 1.0, 1),
        ("A", "Z", 1.0, 1), ("B", "Z", 1.0, 1),  # Z always loses
    ]
    fit = ordo_fit(["A", "B", "Z"], encs,
                   wins={"A": 1, "B": 1},
                   losses={"A": 1, "B": 1, "Z": 2})
    assert fit["Z"] == (None, None)
    # A and B fit; if NumberReplacer changed the wins default, A or B
    # could be mistakenly purged.
    assert fit["A"][0] is not None
    assert fit["B"][0] is not None


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


def test_ordo_fit_purges_engine_with_exactly_one_loss():
    """An all-losses engine with EXACTLY 1 loss must still be purged.
    Kills `losses > 0` -> `losses > 1` NumberReplacer (which would let
    the boundary case losses=1 escape the second purge leg)."""
    # C has 0W 1L (single loss vs A). With the >1 mutation, C is not
    # purged via second leg; we wire C into a real component so the
    # not-purged outcome would yield a real rating.
    encs = [
        ("A", "B", 1.0, 1), ("B", "A", 1.0, 1),  # A, B balanced (1W 1L each)
        ("D", "C", 1.0, 1),                       # D beats C
        ("B", "D", 1.0, 1),                       # B beats D
    ]
    fit = ordo_fit(["A", "B", "C", "D"], encs,
                   wins={"A": 1, "B": 2, "C": 0, "D": 1},
                   losses={"A": 1, "B": 1, "C": 1, "D": 1})
    # C: 0W 1L. Original `losses > 0` fires -> C purged.
    # Mutated `losses > 1` is False -> C kept and would get a rating.
    assert fit["C"] == (None, None)
    # Sanity: B and D fit each other (non-degenerate component after purge).
    assert fit["B"][0] is not None
    assert fit["D"][0] is not None


def test_ordo_fit_all_draws_engine_gets_rating_not_purged():
    """An engine with `wins=0, losses=0` but real draw encounters must
    be FIT (gets a real rating), not purged. Kills `> 0` -> `>= 0`
    mutations on both purge legs (which would fire `0 >= 0` = True
    against any zero-wins/zero-losses engine and silently purge it)."""
    # Z plays 2 draws each vs A and vs B, both colors -> 0W 0L 4D.
    encs = [
        ("A", "B", 1.0, 2), ("B", "A", 1.0, 2),
        ("Z", "A", 1.0, 2), ("A", "Z", 1.0, 2),
        ("Z", "B", 1.0, 2), ("B", "Z", 1.0, 2),
    ]
    fit = ordo_fit(["A", "B", "Z"], encs,
                   wins={"A": 1, "B": 1, "Z": 0},
                   losses={"A": 1, "B": 1, "Z": 0})
    # Z must be in the fit -- not purged via `>= 0` mutation.
    assert fit["Z"][0] is not None


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
    p = {"elo0": 0.0, "elo1": 5.0, "alpha": 0.05, "beta": 0.05}
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
    # 99 pairs A wins both + 1 pair drawn. elo1 is in *normalized* Elo
    # (a stricter scale than logistic), so a large bound is needed for a
    # 100-pair sample to clear the Wald boundary on total domination.
    rc = _RoundCounter()
    body = "".join(_pair(rc, "A", "B", "1-0", "0-1") for _ in range(99))
    body += _pair(rc, "A", "B", "1/2-1/2", "1/2-1/2")
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=50))
    assert r.pairs == 100
    assert r.status == "H1"
    assert r.llr > r.upper_bound


def test_sprt_runaway_b_dominates_accepts_h0(tmp_path):
    # 99 pairs A loses both + 1 pair drawn. Strongly rejects H1.
    rc = _RoundCounter()
    body = "".join(_pair(rc, "A", "B", "0-1", "1-0") for _ in range(99))
    body += _pair(rc, "A", "B", "1/2-1/2", "1/2-1/2")
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=50))
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
    # All pairs in the DD bin (score 1.0). The regularized pentanomial MLE
    # extracts essentially no signal -- LLR sits near zero, well inside the
    # bounds -> continue.
    rc = _RoundCounter()
    body = "".join(_pair(rc, "A", "B", "1/2-1/2", "1/2-1/2") for _ in range(20))
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=5))
    assert r.pairs == 20
    assert r.status == "continue"
    assert abs(r.llr) < 0.05


def test_sprt_all_decisive_same_direction_returns_continue(tmp_path):
    # Every pair: A sweeps both games (score 2.0, all mass in the WW bin).
    # At the tight normalized elo1=5 bound, 20 pairs don't clear the Wald
    # boundary -> continue (positive LLR, leaning H1 but not concluded).
    rc = _RoundCounter()
    body = "".join(_pair(rc, "A", "B", "1-0", "0-1") for _ in range(20))
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0, elo1=5))
    assert r.pairs == 20
    assert r.status == "continue"
    assert r.llr > 0.0


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


def test_sprt_alpha_beta_defaults_accept_valid_params(tmp_path):
    """Omit alpha/beta from params → defaults (0.05) must be valid.
    Kills NumberReplacers on the default values (e.g. `1.05`, `-0.95`)
    which would trip the (0, 1) range guard and raise ValueError."""
    p = _write_pgn(tmp_path, "")
    # No alpha/beta keys → exercise the .get(..., 0.05) defaults.
    r = compute_sprt(p, {"elo0": 0.0, "elo1": 5.0},
                     engine_a="A", engine_b="B")
    assert r.status == "continue"
    # Bounds computed from default alpha=0.05, beta=0.05.
    assert r.lower_bound == pytest.approx(math.log(0.05 / 0.95))
    assert r.upper_bound == pytest.approx(math.log(0.95 / 0.05))


def test_sprt_single_pair_computes_llr(tmp_path):
    """n=1 pair: fastchess computes from the first pair, so we do too (the
    regularized MLE yields a small finite LLR, not a special-cased zero)."""
    rc = _RoundCounter()
    body = _pair(rc, "A", "B", "1-0", "0-1")
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params())
    assert r.pairs == 1
    assert r.status == "continue"
    assert r.llr == pytest.approx(0.020153121, abs=1e-7)


# The exact-LLR cases below pin the normalized pentanomial GSPRT (a port of
# fastchess' sprt.cpp ITP/MLE) against hand-built pair distributions. They
# guard the MLE machinery from silent numeric drift; the magnitudes are tiny
# because elo1=5 is a small effect on the normalized scale.
def test_sprt_exact_llr_two_pairs(tmp_path):
    rc = _RoundCounter()
    body = (
        _pair(rc, "A", "B", "1-0", "1/2-1/2")        # A wins+draw -> score 1.5
        + _pair(rc, "A", "B", "0-1", "1/2-1/2")      # A loses+draw -> score 0.5
    )
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0.0, elo1=5.0))
    assert r.pairs == 2
    assert r.llr == pytest.approx(-4.1474662e-4, abs=1e-9)


def test_sprt_exact_llr_four_pairs(tmp_path):
    rc = _RoundCounter()
    body = (
        _pair(rc, "A", "B", "1-0", "1/2-1/2")        # score 1.5
        + _pair(rc, "A", "B", "0-1", "1/2-1/2")      # score 0.5
        + _pair(rc, "A", "B", "1-0", "1/2-1/2")      # score 1.5
        + _pair(rc, "A", "B", "0-1", "1/2-1/2")      # score 0.5
    )
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0.0, elo1=5.0))
    assert r.pairs == 4
    assert r.llr == pytest.approx(-8.2887165e-4, abs=1e-9)


def test_sprt_exact_llr_three_pairs(tmp_path):
    rc = _RoundCounter()
    body = (
        _pair(rc, "A", "B", "1-0", "0-1")          # pair score 2.0 (A swept)
        + _pair(rc, "A", "B", "1/2-1/2", "1/2-1/2")  # pair score 1.0 (drawn)
        + _pair(rc, "A", "B", "0-1", "1-0")          # pair score 0.0 (B swept)
    )
    p = _write_pgn(tmp_path, body)
    r = _sprt(p, _params(elo0=0.0, elo1=5.0))
    assert r.pairs == 3
    assert r.llr == pytest.approx(-6.2163390e-4, abs=1e-9)


def test_sprt_to_dict_round_trip(tmp_path):
    p = _write_pgn(tmp_path, "")
    d = _sprt(p, _params()).to_dict()
    assert set(d.keys()) == {
        "llr", "lower_bound", "upper_bound", "status",
        "pairs", "elo0", "elo1",
    }


# ---------------------------------------------------------------------------
# Real-run SPRT fixtures: two Sturddle self-play tournaments, movetext stripped
# (SPRT reads only the W/B/Result/Round headers, so this is byte-for-byte
# equivalent to the full PGNs at 1/30th the size). Both were RUN by fastchess
# with model=logistic and concluded (15elo->H1, 20elo->H0). Recomputing with
# the same model reproduces fastchess' verdict to ~1e-4: our LLR lands just
# past +-2.94, exactly where fastchess stopped. This is the ground-truth check
# that the ITP/MLE port matches fastchess.
#
# The normalized recompute of the same data sits at +2.52 / -0.57 (continue):
# correct too, just a stricter Elo scale -- which is why we must recompute with
# the model the run actually used, not a fixed one.
# ---------------------------------------------------------------------------

_SPRT_FIXTURE_A = "Sturddle 2.5.2.061926"
_SPRT_FIXTURE_B = "Sturddle 2.5.0"


def test_sprt_fixture_15elo_logistic_accepts_h1():
    # fastchess ran this logistic and accepted H1; our logistic LLR clears +2.94.
    p = Path(__file__).parent / "fixtures" / "sprt_run_15elo.pgn"
    r = compute_sprt(p, {"elo0": 0.0, "elo1": 15.0, "model": "logistic"},
                     engine_a=_SPRT_FIXTURE_A, engine_b=_SPRT_FIXTURE_B)
    assert r.pairs == 577
    assert r.status == "H1"
    assert r.llr == pytest.approx(2.9711, abs=1e-3)


def test_sprt_fixture_20elo_logistic_accepts_h0():
    # fastchess ran this logistic and accepted H0; our logistic LLR clears -2.94.
    p = Path(__file__).parent / "fixtures" / "sprt_run_20elo.pgn"
    r = compute_sprt(p, {"elo0": 0.0, "elo1": 20.0, "model": "logistic"},
                     engine_a=_SPRT_FIXTURE_A, engine_b=_SPRT_FIXTURE_B)
    assert r.pairs == 280
    assert r.status == "H0"
    assert r.llr == pytest.approx(-3.1443, abs=1e-3)


def test_sprt_fixture_model_dispatch_differs():
    # The same data under normalized stays continue (stricter scale): proves
    # compute_sprt dispatches on the model rather than hardcoding one.
    p = Path(__file__).parent / "fixtures" / "sprt_run_15elo.pgn"
    norm = compute_sprt(p, {"elo0": 0.0, "elo1": 15.0, "model": "normalized"},
                        engine_a=_SPRT_FIXTURE_A, engine_b=_SPRT_FIXTURE_B)
    assert norm.status == "continue"
    assert norm.llr == pytest.approx(2.5186, abs=1e-3)


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


def test_standings_counts_round_number_reuse(tmp_path):
    # Round 1 contains TWO complete color-flipped pairs of the same engine
    # set -- the Round-number-reuse case from a Pause/Resume boundary. All
    # 4 games must count (the old (round, white, black) dedup mis-handled it).
    body = (
        _game_round("1", "A", "B", "1-0") + _game_round("1", "B", "A", "0-1")
        + _game_round("1", "A", "B", "0-1") + _game_round("1", "B", "A", "1-0")
    )
    p = _write_pgn(tmp_path, body)
    assert compute_standings(p).games == 4


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


def test_read_game_record_missing_file_returns_none(tmp_path):
    """Nonexistent PGN path → return None (not raise). Kills
    ExceptionReplacer on the `FileNotFoundError` catch (replacing with
    a non-parent exception class would let the error propagate)."""
    p = tmp_path / "absent.pgn"
    assert read_game_record(p, 1) is None


def test_read_game_record_out_of_range_returns_none(tmp_path):
    """game_n past end of file → None. Kills NotEq/Gt mutations on
    the `game_n > len(offsets)` bounds check."""
    body = _game("A", "B", "1-0")
    p = _write_pgn(tmp_path, body)
    assert read_game_record(p, 2) is None
    assert read_game_record(p, 99) is None


def test_read_game_record_zero_or_negative_game_n_returns_none(tmp_path):
    """game_n < 1 → None. Pins the `< 1` guard."""
    body = _game("A", "B", "1-0")
    p = _write_pgn(tmp_path, body)
    assert read_game_record(p, 0) is None
    assert read_game_record(p, -1) is None


def test_read_game_record_seeks_to_correct_game_for_n_greater_than_two(tmp_path):
    """Reading game 3 must return game 3, not game 1 (`game_n - 1`
    indexing into offsets). Kills `-` → `>>` mutation: `3 - 1 = 2`
    but `3 >> 1 = 1`, which would re-read game 2."""
    body = (
        _game("AAA", "BBB", "1-0")  # game 1
        + _game("CCC", "DDD", "0-1")  # game 2
        + _game("EEE", "FFF", "1/2-1/2")  # game 3
    )
    p = _write_pgn(tmp_path, body)
    rec = read_game_record(p, 3)
    assert rec is not None
    assert rec["engine_white"] == "EEE"
    assert rec["engine_black"] == "FFF"
    assert rec["result"] == "1/2-1/2"


def test_read_game_record_final_fen_reflects_played_moves(tmp_path):
    """final_fen must reflect the position AFTER the last move was played
    (board iteration over mainline). Kills ZeroIterationForLoop on the
    walk_mainline loop (would leave board=None → final_fen=initial pos)
    and IsNot mutation on the `if board is None:` reset guard
    (would overwrite the iterated board with the initial one)."""
    body = _game("A", "B", "1-0")  # contains `1. e4 e5 1-0`
    p = _write_pgn(tmp_path, body)
    rec = read_game_record(p, 1)
    assert rec is not None
    # Initial position FEN; final_fen must NOT equal this.
    initial_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert rec["final_fen"] != initial_fen
    # last_move must be set (loop iterated at least once).
    assert rec["last_move"] is not None


def test_read_game_record_summary_question_mark_names_become_none(tmp_path):
    """White or Black tag value of '?' (the PGN unknown sentinel) must
    map to None in the summary dict. Kills `!=`→`is`/`>`/`>=` mutations
    on the `white != '?'` / `black != '?'` checks."""
    # Build a game with explicit "?" tags (override _game helper).
    body = (
        '[Event "x"]\n[White "?"]\n[Black "?"]\n'
        '[Result "1-0"]\n\n1. e4 e5 1-0\n\n'
    )
    p = _write_pgn(tmp_path, body)
    rec = read_game_record(p, 1)
    assert rec is not None
    assert rec["summary"]["white"] is None
    assert rec["summary"]["black"] is None
    # Engine-level headers fall back to "?" themselves (not None).
    assert rec["engine_white"] == "?" and rec["engine_black"] == "?"


def test_read_game_record_cache_invalidates_when_size_grows(tmp_path):
    """Cache-validity predicate `cached[0] == st.st_mtime_ns AND
    cached[1] == st.st_size` must reject a stale cache when the file
    grew. Forcing st_mtime_ns equality while the file actually grew
    isolates the size-check half of the AND: kills `==` -> `<=`/`<`
    mutations on `cached[1] == st.st_size` which would stale-hit and
    return outdated offsets."""
    from sturddle_view.tournament import pgn_stats as _mod

    body1 = _game("A", "B", "1-0")
    p = _write_pgn(tmp_path, body1)
    rec1 = read_game_record(p, 1)
    assert rec1 is not None
    # Append a 2nd game and force-restore the mtime so the size check
    # is the only differing predicate.
    body2 = body1 + _game("C", "D", "0-1")
    p.write_text(body2, encoding="utf-8")
    # Patch the cache entry: pretend the cached mtime equals the new file
    # mtime (so only the size check could falsely accept it).
    new_st = p.stat()
    _mod._game_offsets_cache[p] = (new_st.st_mtime_ns,
                                   _mod._game_offsets_cache[p][1],
                                   _mod._game_offsets_cache[p][2])

    # Size differs, mtime matches -> cache must miss via size check.
    rec2 = read_game_record(p, 2)
    assert rec2 is not None
    assert rec2["engine_white"] == "C"
    assert rec2["engine_black"] == "D"


def test_read_game_record_cache_invalidates_when_mtime_changes(tmp_path):
    """Force cached size to match the new file size while mtime really
    differs. Replace the file content but keep the byte layout such that
    the cached offsets WOULD return a different game from offset 0
    than the recomputed offsets do. Kills `==` -> `<=`/`<` mutations on
    `cached[0] == st.st_mtime_ns`."""
    from sturddle_view.tournament import pgn_stats as _mod
    import time as _time

    g1 = _game("A", "B", "1-0")
    p = _write_pgn(tmp_path, g1)
    rec1 = read_game_record(p, 1)
    assert rec1 is not None
    assert rec1["engine_white"] == "A"

    # body2: same total length, but with a *non-decisive* first game
    # (Result "*"). Recomputed offsets would have zero entries (the
    # `*` game is skipped); stale-hit cached offsets still report [0]
    # and would return the new game's headers ("X" / "Y").
    g_ongoing = (
        '[Event "x"]\n[White "X"]\n[Black "Y"]\n'
        '[Result "*"]\n\n1. e4 e5 *\n\n'
    )
    pad_len = len(g1) - len(g_ongoing)
    assert pad_len >= 0
    g_padded = g_ongoing + " " * pad_len  # exactly len(g1) bytes
    assert len(g_padded) == len(g1)

    _time.sleep(0.01)
    p.write_text(g_padded, encoding="utf-8")
    new_st = p.stat()
    _mod._game_offsets_cache[p] = (_mod._game_offsets_cache[p][0],
                                   new_st.st_size,
                                   _mod._game_offsets_cache[p][2])

    # Recomputed offsets = [] (the only game has Result "*"). game_n=1
    # is out of range -> None. With stale-hit (mutated mtime check),
    # cached offsets [0] would seek to 0 and read the "*"-result game
    # -> non-None (engine_white="X").
    rec2 = read_game_record(p, 1)
    assert rec2 is None


# ---------------------------------------------------------------------------
# games_played_from_config
# ---------------------------------------------------------------------------


def test_games_played_from_config_real_fixture():
    # Snapshot of a real resumed tournament's fastchess config.json,
    # stripped to the only field this helper reads. wins+losses+draws
    # = 677 + 539 + 2817 = 4033, matching the "Started game 4034 of N"
    # observed on resume.
    assert games_played_from_config(_FIXTURE_RESUME_CONFIG) == 4033


def test_games_played_from_config_sums_across_pairs(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "stats": {
            "A vs B": {"wins": 3, "losses": 2, "draws": 5},
            "C vs D": {"wins": 1, "losses": 0, "draws": 4},
        }
    }), encoding="utf-8")
    assert games_played_from_config(p) == 15


def test_games_played_from_config_missing_returns_none(tmp_path):
    assert games_played_from_config(tmp_path / "nope.json") is None


def test_games_played_from_config_empty_stats(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"stats": {}}), encoding="utf-8")
    assert games_played_from_config(p) == 0


def test_games_played_from_config_malformed_returns_none(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ not json", encoding="utf-8")
    assert games_played_from_config(p) is None


def test_games_played_from_config_null_field_treated_as_zero(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "stats": {"A vs B": {"wins": None, "losses": 2, "draws": 3}}
    }), encoding="utf-8")
    assert games_played_from_config(p) == 5


def test_games_played_from_config_non_numeric_field_returns_none(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "stats": {"A vs B": {"wins": "many", "losses": 0, "draws": 0}}
    }), encoding="utf-8")
    assert games_played_from_config(p) is None


_NAJDORF = "1. e4 c5 2. Nf3 d6 3. d4 cxd4 4. Nxd4 Nf6 5. Nc3 a6"
_QGD = "1. d4 d5 2. c4 e6"


def _game_moves(white: str, black: str, result: str, moves: str) -> str:
    return (
        f'[Event "x"]\n[White "{white}"]\n[Black "{black}"]\n'
        f'[Round "1"]\n[Result "{result}"]\n\n{moves} {result}\n\n'
    )


def test_games_list_attaches_identified_opening(tmp_path):
    body = _game_moves("A", "B", "1-0", _NAJDORF) + _game_moves("B", "A", "1/2-1/2", _QGD)
    games = compute_games_list(_write_pgn(tmp_path, body))
    assert [(g["white"], g["black"], g["result"]) for g in games] == [
        ("A", "B", "1-0"), ("B", "A", "1/2-1/2")]
    assert "Najdorf" in games[0]["opening"]
    assert games[1]["opening"] == "Queen's Gambit Declined"


def test_games_list_opening_blank_for_moveless_game(tmp_path):
    body = '[Event "x"]\n[White "A"]\n[Black "B"]\n[Round "1"]\n[Result "1-0"]\n\n1-0\n\n'
    games = compute_games_list(_write_pgn(tmp_path, body))
    assert games[0]["opening"] == ""


def test_games_list_missing_file_returns_empty(tmp_path):
    assert compute_games_list(tmp_path / "absent.pgn") == []


def test_games_list_row_index_matches_replay_game_number(tmp_path):
    body = _game("A", "B", "1-0") + _game("C", "D", "0-1") + _game("E", "F", "1-0")
    p = _write_pgn(tmp_path, body)
    games = compute_games_list(p)
    for i, g in enumerate(games, start=1):
        rec = read_game_record(p, i)
        assert (g["white"], g["black"], g["result"]) == (
            rec["engine_white"], rec["engine_black"], rec["result"])


def test_games_list_served_from_cache_when_unchanged(tmp_path):
    from sturddle_view.tournament import pgn_stats as _mod

    p = _write_pgn(tmp_path, _game_moves("A", "B", "1-0", _NAJDORF))
    first = compute_games_list(p)
    assert _mod._games_list_cache[p][2] is first
    assert compute_games_list(p) is first  # unchanged file -> same object


def test_games_list_append_reuses_memo_for_prior_games(tmp_path):
    # A live tournament grows the PGN each game; the existing game's record
    # must be reused from _opening_memo (same object), not re-parsed.
    g1 = _game_moves("A", "B", "1-0", _NAJDORF)
    p = _write_pgn(tmp_path, g1)
    r0 = compute_games_list(p)[0]
    p.write_text(g1 + _game_moves("B", "A", "0-1", _QGD), encoding="utf-8")
    grown = compute_games_list(p)
    assert grown[0] is r0  # memoized, not recomputed
    assert len(grown) == 2
    assert grown[1]["opening"] == "Queen's Gambit Declined"


def test_forget_drops_memo_so_wipe_reparses(tmp_path):
    # Stop/restart wipes the PGN and reuses offset 0 for a new game. forget()
    # (called by the wipe path) is the contract that invalidates the memo;
    # without it the append-only invariant breaks and offset 0 goes stale.
    from sturddle_view.tournament import pgn_stats as _mod

    p = _write_pgn(tmp_path, _game_moves("A", "B", "1-0", _NAJDORF))
    assert "Najdorf" in compute_games_list(p)[0]["opening"]
    _mod.forget(p)
    assert not any(k[0] == p for k in _mod._opening_memo)
    p.write_text(_game_moves("C", "D", "0-1", _QGD), encoding="utf-8")
    after = compute_games_list(p)[0]
    assert after["white"] == "C"
    assert after["opening"] == "Queen's Gambit Declined"


