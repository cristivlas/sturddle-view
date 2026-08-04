"""Integration tests for HvE difficulty blinding (levels 1-9).

A duck-typed stub engine serves the shallow sweep (analyse), the probe
(play), and the restricted real search (analysis), so the whole flow
runs without a UCI subprocess: probe caching/degrade+toast, off-clock
sweep, pool restriction of the real search, and the untouched level-10
path. Blinding randomness is bypassed by monkeypatching candidate_pool
where determinism matters; no waits anywhere.
"""
from __future__ import annotations

from types import SimpleNamespace

import chess
import chess.engine

from sturddle_view.config import (
    _DEFAULT_HVE_SWEEP_BUDGET_SECONDS,
    _DEFAULT_HVE_SWEEP_MOVETIME_SECONDS,
)
from sturddle_view.events import (
    EVT_ENGINE_SEARCH_START,
    EVT_SYSTEM,
    EventBus,
)
from sturddle_view.play import human_vs_engine as hve_mod
from sturddle_view.play.chess_clock import ChessClock, TimeControl
from sturddle_view.play.difficulty import DIFFICULTY_UNAVAILABLE_ERROR
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.play.opening_lines import BookRef

# White: Ka1, Pa2; Black: Ka8. Exactly four legal moves.
FOUR_MOVE_FEN = "k7/8/8/8/8/8/P7/K7 w - - 0 1"
# Mover-POV shallow scores per uci.
SCORES = {"a1b1": 0, "a1b2": -30, "a2a3": -100, "a2a4": -600}

LOG = "sturddle_view.play.human_vs_engine"


class FakeAnalysis:
    """Mimics chess.engine.AnalysisResult where the driver touches it."""

    def __init__(self, infos, best_move):
        self._infos = list(infos)
        self._best = best_move

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._infos:
            raise StopAsyncIteration
        return self._infos.pop(0)

    def stop(self):
        pass

    async def wait(self):
        return SimpleNamespace(move=self._best, ponder=None)


class BlindableEngine:
    """Duck-typed UciProtocol covering all three difficulty calls.

    analyse() = shallow sweep (single root move, scripted score);
    analysis() = the real search, returning the best-scoring move of
    root_moves (or of all legal moves when unrestricted);
    play() = the searchmoves probe.
    """

    def __init__(self, scores, *, honors_searchmoves=True):
        self.scores = dict(scores)
        self.honors = honors_searchmoves
        self.play_calls = 0
        self.sweep_calls: list[str] = []
        self.sweep_limits: list[float] = []
        self.search_pools: list[list[str] | None] = []
        self.on_sweep = None  # hook fired per analyse() call

    async def play(self, board, limit, *, root_moves=None, **kw):
        self.play_calls += 1
        assert root_moves, "probe must restrict the search"
        if self.honors:
            return chess.engine.PlayResult(root_moves[0], None)
        other = next(m for m in board.legal_moves if m != root_moves[0])
        return chess.engine.PlayResult(other, None)

    async def analyse(self, board, limit, *, root_moves=None, **kw):
        assert root_moves is not None and len(root_moves) == 1
        mv = root_moves[0]
        self.sweep_calls.append(mv.uci())
        self.sweep_limits.append(limit.time)
        if self.on_sweep is not None:
            self.on_sweep()
        score = chess.engine.PovScore(
            chess.engine.Cp(self.scores[mv.uci()]), board.turn,
        )
        return {"score": score, "depth": 5}

    async def analysis(self, board, limit=None, *, root_moves=None, **kw):
        self.search_pools.append(
            None if root_moves is None else [m.uci() for m in root_moves]
        )
        allowed = list(root_moves) if root_moves else list(board.legal_moves)
        mv = max(allowed, key=lambda m: self.scores[m.uci()])
        info = {
            "score": chess.engine.PovScore(
                chess.engine.Cp(self.scores[mv.uci()]), board.turn,
            ),
            "pv": [mv],
            "depth": 20,
        }
        return FakeAnalysis([info], mv)


def _make(scores=SCORES, *, level=5, honors=True, clock=None):
    settings = SimpleNamespace(hve_difficulty=level)
    hve = HumanVsEngine(engine_path="/fake/engine", bus=EventBus(), settings=settings)
    engine = BlindableEngine(scores, honors_searchmoves=honors)
    hve._supervisor.engine = engine
    hve._board = chess.Board(FOUR_MOVE_FEN)
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


def _fix_pool(monkeypatch, indices):
    """Bypass blinding randomness: candidate_pool returns `indices`."""
    monkeypatch.setattr(
        hve_mod, "candidate_pool", lambda *a, **kw: list(indices),
    )


def _drain(events):
    return [events.get_nowait() for _ in range(events.qsize())]


# ----- sweep + restricted search flow -----

async def test_sweep_scores_all_then_search_restricted(monkeypatch):
    """Every legal move swept once; the real search sees the pool."""
    hve, engine = _make()
    _fix_pool(monkeypatch, [1, 2])  # blind the best move
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    legal = [m.uci() for m in chess.Board(FOUR_MOVE_FEN).legal_moves]
    assert engine.play_calls == 1  # probe
    assert engine.sweep_calls == legal
    assert engine.search_pools == [[legal[1], legal[2]]]
    # The engine plays its best VISIBLE move at full depth.
    (move, captured), = commits
    assert move.uci() == max(
        engine.search_pools[0], key=lambda u: SCORES[u],
    )
    # Eval history carries the restricted search's real deep score.
    assert captured == {"cp": SCORES[move.uci()], "depth": 20}


async def test_full_pool_restricts_to_all_admitted(monkeypatch):
    """Nothing blinded: the search still receives the pool (harmless
    restriction to every admitted move)."""
    hve, engine = _make()
    _fix_pool(monkeypatch, [0, 1, 2, 3])
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.search_pools == [[m.uci() for m in chess.Board(FOUR_MOVE_FEN).legal_moves]]
    assert commits[0][0].uci() == "a1b1"  # best visible = true best


async def test_sweep_time_spreads_budget_over_moves(monkeypatch):
    """Per-candidate movetime is max(floor, budget / legal moves):
    four legal moves make the budget share beat the floor."""
    hve, engine = _make()
    _fix_pool(monkeypatch, [0])
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    expected = max(
        _DEFAULT_HVE_SWEEP_MOVETIME_SECONDS,
        _DEFAULT_HVE_SWEEP_BUDGET_SECONDS / 4,
    )
    assert engine.sweep_limits == [expected] * 4


# ----- probe: caching, degrade -----

async def test_probe_cached_across_engine_turns(monkeypatch):
    hve, engine = _make()
    _fix_pool(monkeypatch, [0])
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    await hve._think_and_play()
    assert engine.play_calls == 1


async def test_reprobe_after_engine_respawn(monkeypatch):
    hve, engine = _make()
    _fix_pool(monkeypatch, [0])
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    engine2 = BlindableEngine(SCORES)
    hve._supervisor.engine = engine2
    await hve._think_and_play()
    assert engine.play_calls == 1
    assert engine2.play_calls == 1


async def test_degrade_to_full_strength_with_toast(caplog, monkeypatch):
    """Probe failure: toast once per engine process, no sweep, and the
    real search runs unrestricted."""
    hve, engine = _make(honors=False)
    commits = _capture_commit(hve, monkeypatch)
    events = await hve._bus.subscribe()
    with caplog.at_level("WARNING", logger=LOG):
        await hve._think_and_play()
        await hve._think_and_play()
    assert any("difficulty unavailable" in m for m in caplog.messages)
    toasts = [
        e for e in _drain(events)
        if e.kind == EVT_SYSTEM and e.payload.get("error") == DIFFICULTY_UNAVAILABLE_ERROR
    ]
    assert len(toasts) == 1
    assert engine.sweep_calls == []
    assert engine.search_pools == [None, None]
    assert [c[0].uci() for c in commits] == ["a1b1", "a1b1"]


# ----- clock -----

async def test_sweep_is_off_clock_search_is_on_clock():
    """Fake monotonic: 0.5s per sweep call never reaches the clock;
    only increment credit shows at commit (search itself takes zero
    fake time)."""
    fake = SimpleNamespace(t=0.0)
    clock = ChessClock(
        TimeControl(initial_seconds=60, increment_seconds=2),
        monotonic=lambda: fake.t,
    )
    hve, engine = _make(level=1, clock=clock)

    def advance():
        fake.t += 0.5

    engine.on_sweep = advance
    clock.start_turn()
    await hve._think_and_play()  # real commit path
    assert fake.t == 2.0  # sweep consumed fake time
    assert hve._clock.white_time == 62.0  # 60 - 0 elapsed + 2 increment
    assert hve._clock.black_time == 60.0
    assert len(hve._board.move_stack) == 1


# ----- cancellation -----

async def test_cancel_mid_sweep_commits_nothing(monkeypatch):
    hve, engine = _make()
    engine.on_sweep = lambda: setattr(hve, "_think_gen", hve._think_gen + 1)
    monkeypatch.setattr(
        hve, "_commit_engine_move", _forbidden("cancelled sweep must not commit"),
    )
    await hve._think_and_play()  # must not raise


# ----- path selection -----

async def test_full_strength_skips_probe_sweep_and_pool(monkeypatch):
    hve, engine = _make(level=10)
    monkeypatch.setattr(
        hve, "_probe_searchmoves", _forbidden("level 10 must not probe"),
    )
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.sweep_calls == []
    assert engine.search_pools == [None]
    assert [c[0].uci() for c in commits] == ["a1b1"]


async def test_no_settings_means_full_strength(monkeypatch):
    hve, _ = _make()
    hve._settings = None
    monkeypatch.setattr(
        hve, "_probe_searchmoves", _forbidden("no settings must mean full strength"),
    )
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()


async def test_book_move_bypasses_blinding_below_max(monkeypatch):
    hve, engine = _make(level=5)
    hve._board = chess.Board()
    hve._board.push(chess.Move.from_uci("e2e4"))
    hve._book = BookRef(path="book.pgn", plies=None, order=None, anchor=0)
    monkeypatch.setattr(hve_mod, "book_reply", lambda *a: "e7e5")
    monkeypatch.setattr(
        hve, "_probe_searchmoves", _forbidden("book moves play at full strength"),
    )
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.sweep_calls == []
    assert [c[0].uci() for c in commits] == ["e7e5"]


# ----- events -----

async def test_search_start_published_before_sweep(monkeypatch):
    hve, _ = _make()
    _fix_pool(monkeypatch, [0])
    _capture_commit(hve, monkeypatch)
    events = await hve._bus.subscribe()
    await hve._think_and_play()
    kinds = [e.kind for e in _drain(events)]
    assert kinds.count(EVT_ENGINE_SEARCH_START) == 2  # pre-sweep + search
