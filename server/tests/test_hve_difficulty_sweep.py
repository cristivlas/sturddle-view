"""Integration tests for the HvE difficulty sweep (levels 1-9 path).

A duck-typed stub engine scripts per-candidate scores, so every branch
runs without a UCI subprocess: searchmoves vs push-and-eval fallback,
probe caching/re-probe, terminal positions, cancellation, clock
charging (fake monotonic -- zero waits), and the published event
stream. Deterministic move choices use score gaps wider than the drop
cap, never rng seeds.
"""
from __future__ import annotations

from types import SimpleNamespace

import chess
import chess.engine
import pytest

from sturddle_view.events import (
    EVT_ENGINE_INFO,
    EVT_ENGINE_SEARCH_START,
    EventBus,
)
from sturddle_view.play import human_vs_engine as hve_mod
from sturddle_view.play.chess_clock import ChessClock, TimeControl
from sturddle_view.play.difficulty import PROBE_FEN
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.play.opening_lines import BookRef

# White: Ka1, Pa2; Black: Ka8. Exactly four legal moves.
FOUR_MOVE_FEN = "k7/8/8/8/8/8/P7/K7 w - - 0 1"
# Mover-POV scores; gaps small enough that level 5 keeps several
# candidates eligible.
SCORES_SPREAD = {"a1b1": 0, "a1b2": -30, "a2a3": -100, "a2a4": -600}
# Gaps wider than any level's drop cap => sampling is deterministic.
SCORES_GAP = {"a1b1": 0, "a1b2": -500, "a2a3": -600, "a2a4": -700}
BEST_GAP_MOVE = chess.Move.from_uci("a1b1")

# Black mirror of PROBE_FEN: Ra8-a1 is back-rank mate against Kg1.
PROBE_FEN_BLACK = "r5k1/5ppp/8/8/8/8/5PPP/6K1 b - - 0 1"

# Black: Kh8; White: Rf7, Kg6. Kg8 is the only legal move.
ONE_MOVE_FEN = "7k/5R2/6K1/8/8/8/8/8 b - - 0 1"

LOG = "sturddle_view.play.human_vs_engine"


class SweepEngine:
    """Duck-typed UciProtocol driving the sweep with scripted scores.

    `scores` maps candidate uci -> mover-POV cp at the root; a value of
    None scripts a search that reports no score. analyse() mirrors the
    real protocol's POV convention: relative to the searched position's
    side to move (hence the negation on the fallback/reply position).
    """

    def __init__(self, scores, *, honors_searchmoves=True, default_cp=None):
        self.scores = dict(scores)
        self.honors = honors_searchmoves
        self.default_cp = default_cp
        self.play_calls = 0
        self.analyse_root_moves: list[list[str]] = []
        self.on_analyse = None  # optional hook, called before scoring

    def _cp(self, uci: str):
        if uci in self.scores:
            return self.scores[uci]
        assert self.default_cp is not None, f"unscripted candidate {uci}"
        return self.default_cp

    async def play(self, board, limit, *, root_moves=None, **kw):
        self.play_calls += 1
        assert root_moves, "probe must restrict the search"
        if self.honors:
            return chess.engine.PlayResult(root_moves[0], None)
        other = next(m for m in board.legal_moves if m != root_moves[0])
        return chess.engine.PlayResult(other, None)

    async def analyse(self, board, limit, *, root_moves=None, **kw):
        self.analyse_root_moves.append([m.uci() for m in (root_moves or [])])
        if self.on_analyse is not None:
            self.on_analyse()
        if root_moves:
            mv = root_moves[0]
            cp = self._cp(mv.uci())
            if cp is None:
                return {"depth": 5}
            score = chess.engine.PovScore(chess.engine.Cp(cp), board.turn)
            return {"score": score, "pv": [mv], "depth": 5}
        cp = self._cp(board.peek().uci())
        if cp is None:
            return {"depth": 5}
        score = chess.engine.PovScore(chess.engine.Cp(-cp), board.turn)
        return {"score": score, "pv": [], "depth": 5}

    async def analysis(self, *args, **kw):
        raise RuntimeError("full-strength path must not run in sweep tests")


def _make(scores, *, level=5, honors=True, fen=None, default_cp=None, clock=None):
    settings = SimpleNamespace(hve_difficulty=level, hve_think_delay_seconds=0.0)
    hve = HumanVsEngine(engine_path="/fake/engine", bus=EventBus(), settings=settings)
    engine = SweepEngine(scores, honors_searchmoves=honors, default_cp=default_cp)
    hve._supervisor.engine = engine
    hve._board = chess.Board(fen) if fen else chess.Board(FOUR_MOVE_FEN)
    hve._game_id = "g1"
    if clock is not None:
        hve._clock = clock
    return hve, engine


def _capture_commit(hve, monkeypatch):
    commits = []

    async def commit(move, captured, game_id, gen):
        commits.append((move, captured))

    monkeypatch.setattr(hve, "_commit_engine_move", commit)
    return commits


def _forbidden(message):
    def raiser(*args, **kw):
        raise AssertionError(message)
    return raiser


# ----- searchmoves path -----

async def test_sweep_scores_every_candidate_via_searchmoves(monkeypatch):
    hve, engine = _make(SCORES_SPREAD)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    legal = {m.uci() for m in chess.Board(FOUR_MOVE_FEN).legal_moves}
    assert engine.play_calls == 1  # probe
    assert all(len(r) == 1 for r in engine.analyse_root_moves)
    assert {r[0] for r in engine.analyse_root_moves} == legal
    assert len(commits) == 1
    assert commits[0][0].uci() in legal


async def test_probe_cached_across_engine_turns(monkeypatch):
    hve, engine = _make(SCORES_SPREAD)
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    await hve._think_and_play()
    assert engine.play_calls == 1


async def test_reprobe_after_engine_respawn(monkeypatch):
    hve, engine = _make(SCORES_SPREAD)
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    engine2 = SweepEngine(SCORES_SPREAD)
    hve._supervisor.engine = engine2
    await hve._think_and_play()
    assert engine.play_calls == 1
    assert engine2.play_calls == 1


@pytest.mark.parametrize("level", [1, 9])
async def test_gap_scores_pick_best_deterministically(level, monkeypatch):
    """Every non-best candidate is behind by more than the widest drop
    cap, so at any level only the best move is sampleable."""
    hve, _ = _make(SCORES_GAP, level=level)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert [c[0] for c in commits] == [BEST_GAP_MOVE]


# ----- fallback path -----

async def test_fallback_when_searchmoves_ignored(caplog, monkeypatch):
    hve, engine = _make(SCORES_GAP, honors=False)
    commits = _capture_commit(hve, monkeypatch)
    with caplog.at_level("WARNING", logger=LOG):
        await hve._think_and_play()
    assert any("ignores searchmoves" in m for m in caplog.messages)
    # Fallback analyses the pushed reply position with no restriction.
    assert engine.analyse_root_moves
    assert all(r == [] for r in engine.analyse_root_moves)
    assert [c[0] for c in commits] == [BEST_GAP_MOVE]


async def test_fallback_scores_mate_without_engine_white(monkeypatch):
    """A mating candidate never reaches the engine: terminal positions
    are scored directly. White mates => eval entry mate +1."""
    hve, engine = _make({}, level=9, honors=False, fen=PROBE_FEN, default_cp=-400)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert [c[0].uci() for c in commits] == ["a1a8"]
    assert commits[0][1] == {"mate": 1}
    # The mate move itself must not have been sent to the engine.
    assert all(r == [] for r in engine.analyse_root_moves)


async def test_fallback_scores_mate_without_engine_black(monkeypatch):
    hve, _ = _make({}, level=9, honors=False, fen=PROBE_FEN_BLACK, default_cp=-400)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert [c[0].uci() for c in commits] == ["a8a1"]
    assert commits[0][1] == {"mate": -1}


async def test_no_score_info_treated_as_equal(caplog, monkeypatch):
    """A candidate whose search reports no score counts as 0cp -- here
    that makes it the best move, proving the 0 (not worst) semantics."""
    scores = {"a1b1": None, "a1b2": -500, "a2a3": -600, "a2a4": -700}
    hve, _ = _make(scores, level=9)
    commits = _capture_commit(hve, monkeypatch)
    with caplog.at_level("WARNING", logger=LOG):
        await hve._think_and_play()
    assert any("no score" in m for m in caplog.messages)
    assert [c[0] for c in commits] == [BEST_GAP_MOVE]
    assert commits[0][1] == {"cp": 0}


# ----- eval history POV -----

async def test_single_legal_move_and_black_pov_entry(monkeypatch):
    """One legal move: swept, committed, and its mover-POV -50 lands in
    eval history as white-POV +50."""
    hve, _ = _make({"h8g8": -50}, fen=ONE_MOVE_FEN)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert [c[0].uci() for c in commits] == ["h8g8"]
    assert commits[0][1] == {"cp": 50}


# ----- published events -----

async def test_engine_info_published_per_candidate(monkeypatch):
    hve, _ = _make(SCORES_SPREAD)
    _capture_commit(hve, monkeypatch)
    events = await hve._bus.subscribe()
    await hve._think_and_play()
    seen = [events.get_nowait() for _ in range(events.qsize())]
    infos = [e for e in seen if e.kind == EVT_ENGINE_INFO]
    moves = [m.uci() for m in chess.Board(FOUR_MOVE_FEN).legal_moves]
    assert seen[0].kind == EVT_ENGINE_SEARCH_START
    assert len(infos) == len(moves)
    assert [e.payload["depth"] for e in infos] == list(range(1, len(moves) + 1))
    assert [e.payload["pv_uci"][0] for e in infos] == moves
    # White to move + white-POV default: cp equals the scripted score.
    assert [e.payload["score"]["cp"] for e in infos] == [
        SCORES_SPREAD[u] for u in moves
    ]


# ----- cancellation -----

async def test_cancel_mid_sweep_commits_nothing(monkeypatch):
    hve, engine = _make(SCORES_SPREAD)
    engine.on_analyse = lambda: setattr(hve, "_think_gen", hve._think_gen + 1)
    monkeypatch.setattr(
        hve, "_commit_engine_move", _forbidden("cancelled sweep must not commit"),
    )
    await hve._think_and_play()  # must not raise


# ----- clock -----

async def test_clock_charges_only_cosmetic_delay():
    """Fake monotonic: each candidate search 'takes' 0.5s, yet the
    engine is debited nothing and credited the increment (delay=0)."""
    fake = SimpleNamespace(t=0.0)
    clock = ChessClock(
        TimeControl(initial_seconds=60, increment_seconds=2),
        monotonic=lambda: fake.t,
    )
    hve, engine = _make(SCORES_SPREAD, clock=clock)

    def advance():
        fake.t += 0.5

    engine.on_analyse = advance
    clock.start_turn()
    await hve._think_and_play()  # real commit: debits clock, pushes move
    assert fake.t == 2.0  # sanity: the sweep did consume fake time
    assert hve._clock.white_time == 62.0
    assert hve._clock.black_time == 60.0
    assert len(hve._board.move_stack) == 1


# ----- path selection -----

async def test_full_strength_skips_sweep(monkeypatch):
    hve, _ = _make(SCORES_SPREAD, level=10)
    monkeypatch.setattr(
        hve, "_sweep_and_play", _forbidden("level 10 must use the full search"),
    )
    await hve._think_and_play()  # stub analysis() raises RuntimeError; swallowed


async def test_no_settings_means_full_strength(monkeypatch):
    hve, _ = _make(SCORES_SPREAD)
    hve._settings = None
    monkeypatch.setattr(
        hve, "_sweep_and_play", _forbidden("no settings must mean full strength"),
    )
    await hve._think_and_play()


async def test_book_move_bypasses_sweep_below_max(monkeypatch):
    hve, _ = _make(SCORES_SPREAD, level=5, fen=None)
    hve._board = chess.Board()
    hve._board.push(chess.Move.from_uci("e2e4"))
    hve._book = BookRef(path="book.pgn", plies=None, order=None, anchor=0)
    monkeypatch.setattr(hve_mod, "book_reply", lambda *a: "e7e5")
    monkeypatch.setattr(
        hve, "_sweep_and_play", _forbidden("book moves play at full strength"),
    )
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert [c[0].uci() for c in commits] == ["e7e5"]
