"""Integration tests for HvE difficulty iteration sampling (levels 1-9).

One scripted search per engine move: a stub analysis emits the
iteration info stream, and below max difficulty the driver samples the
reply from it. Deterministic outcomes use costs beyond the drop cap,
never rng seeds; no waits anywhere.
"""
from __future__ import annotations

from types import SimpleNamespace

import chess
import chess.engine

from sturddle_view.events import (
    EVT_ENGINE_INFO,
    EVT_ENGINE_SEARCH_START,
    EventBus,
)
from sturddle_view.play import human_vs_engine as hve_mod
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.play.opening_lines import BookRef

# White: Ka1, Pa2; Black: Ka8.
FOUR_MOVE_FEN = "k7/8/8/8/8/8/P7/K7 w - - 0 1"

LOG = "sturddle_view.play.human_vs_engine"


def _mv(uci):
    return chess.Move.from_uci(uci)


def _info(depth, uci, cp=None, mate=None, pov=chess.WHITE, **extra):
    score = chess.engine.Mate(mate) if mate is not None else chess.engine.Cp(cp)
    return {
        "depth": depth,
        "score": chess.engine.PovScore(score, pov),
        "pv": [_mv(uci)],
        **extra,
    }


class FakeAnalysis:
    """Mimics chess.engine.AnalysisResult where the driver touches it:
    sync context manager, async info iterator, stop(), wait()."""

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


class ScriptedEngine:
    """Duck-typed UciProtocol: every analysis() replays the scripted
    iteration info stream and returns the scripted bestmove."""

    def __init__(self, infos, best_uci):
        self.infos = infos
        self.best = _mv(best_uci)
        self.calls = 0

    async def analysis(self, board, limit=None, **kw):
        self.calls += 1
        return FakeAnalysis(self.infos, self.best)


def _make(infos, best_uci, *, level=5, fen=FOUR_MOVE_FEN):
    settings = SimpleNamespace(hve_difficulty=level)
    hve = HumanVsEngine(engine_path="/fake/engine", bus=EventBus(), settings=settings)
    engine = ScriptedEngine(infos, best_uci)
    hve._supervisor.engine = engine
    hve._board = chess.Board(fen)
    hve._game_id = "g1"
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


def _drain(events):
    return [events.get_nowait() for _ in range(events.qsize())]


# The engine changed its mind across depths; final best is a1b1.
ITERS_MIND_CHANGE = [
    _info(1, "a2a3", cp=40),
    _info(3, "a1b2", cp=10),
    _info(8, "a1b1", cp=30),
]


# ----- sampling outcomes -----

async def test_high_level_plays_final_best_when_others_stale(monkeypatch):
    """Level 9 (cap 50): a2a3 costs 105 (7 depths stale), a1b2 costs 95
    -- both excluded, the final best is deterministic."""
    hve, _ = _make(ITERS_MIND_CHANGE, "a1b1", level=9)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert [c[0].uci() for c in commits] == ["a1b1"]
    assert commits[0][1] == {"cp": 30, "depth": 8}


async def test_low_level_commits_a_pool_move_with_its_entry(monkeypatch):
    """Level 1 (cap 450): every candidate is eligible; whatever is
    sampled, the committed move and its eval entry are consistent."""
    hve, _ = _make(ITERS_MIND_CHANGE, "a1b1", level=1)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    (move, entry), = commits
    expected = {
        "a2a3": {"cp": 40, "depth": 1},
        "a1b2": {"cp": 10, "depth": 3},
        "a1b1": {"cp": 30, "depth": 8},
    }
    assert entry == expected[move.uci()]


async def test_mate_candidate_entry(monkeypatch):
    hve, _ = _make([_info(6, "a1b1", mate=3)], "a1b1", level=5)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert commits[0][1] == {"mate": 3, "depth": 6}


async def test_filtered_lines_never_enter_the_pool(monkeypatch):
    """Aspiration bounds and MultiPV side lines are invisible to the
    sampler even at the loosest level."""
    infos = [
        _info(2, "a2a4", cp=500, lowerbound=True),
        _info(3, "a2a3", cp=400, multipv=2),
        _info(8, "a1b1", cp=30),
    ]
    hve, _ = _make(infos, "a1b1", level=1)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert [c[0].uci() for c in commits] == ["a1b1"]


async def test_illegal_pv_move_falls_back_to_bestmove(caplog, monkeypatch):
    """A scripted pool whose only candidate is illegal in this position:
    the sampler declines and the engine's bestmove is played."""
    hve, _ = _make([_info(5, "h7h5", cp=20)], "a1b1", level=5)
    commits = _capture_commit(hve, monkeypatch)
    with caplog.at_level("WARNING", logger=LOG):
        await hve._think_and_play()
    assert any("not legal here" in m for m in caplog.messages)
    assert [c[0].uci() for c in commits] == ["a1b1"]


async def test_empty_iteration_stream_plays_bestmove(monkeypatch):
    """No scored iterations (e.g. an engine emitting only currmove
    chatter): the bestmove path runs unchanged."""
    hve, _ = _make([], "a1b1", level=3)
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert [c[0].uci() for c in commits] == ["a1b1"]


# ----- path selection -----

async def test_full_strength_never_samples(monkeypatch):
    hve, engine = _make(ITERS_MIND_CHANGE, "a1b1", level=10)
    monkeypatch.setattr(
        hve, "_pick_difficulty_move", _forbidden("level 10 must play bestmove"),
    )
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.calls == 1
    assert [c[0].uci() for c in commits] == ["a1b1"]


async def test_no_settings_means_full_strength(monkeypatch):
    hve, _ = _make(ITERS_MIND_CHANGE, "a1b1")
    hve._settings = None
    monkeypatch.setattr(
        hve, "_pick_difficulty_move", _forbidden("no settings must mean full strength"),
    )
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()


async def test_book_move_bypasses_sampling_below_max(monkeypatch):
    hve, engine = _make(ITERS_MIND_CHANGE, "a1b1", level=5)
    hve._board = chess.Board()
    hve._board.push(_mv("e2e4"))
    hve._book = BookRef(path="book.pgn", plies=None, order=None, anchor=0)
    monkeypatch.setattr(hve_mod, "book_reply", lambda *a: "e7e5")
    monkeypatch.setattr(
        hve, "_pick_difficulty_move", _forbidden("book moves play at full strength"),
    )
    commits = _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.calls == 0
    assert [c[0].uci() for c in commits] == ["e7e5"]


# ----- events -----

async def test_search_stream_is_the_normal_one(monkeypatch):
    """Sampling changes only the committed move: search start once, one
    info event per scripted line, exactly like level 10."""
    hve, _ = _make(ITERS_MIND_CHANGE, "a1b1", level=5)
    _capture_commit(hve, monkeypatch)
    events = await hve._bus.subscribe()
    await hve._think_and_play()
    seen = _drain(events)
    assert [e.kind for e in seen if e.kind == EVT_ENGINE_SEARCH_START] == [
        EVT_ENGINE_SEARCH_START
    ]
    infos = [e for e in seen if e.kind == EVT_ENGINE_INFO]
    assert len(infos) == len(ITERS_MIND_CHANGE)
