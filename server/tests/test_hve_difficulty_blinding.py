"""Integration tests for HvE difficulty blinding (levels 1-9), driven
by a duck-typed stub engine instead of a UCI subprocess. Blinding
randomness is bypassed by monkeypatching candidate_pool."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import chess
import chess.engine
import pytest

from sturddle_view.config import (
    _DEFAULT_HVE_DEFICIT_RELIEF_CAP,
    _DEFAULT_HVE_DEFICIT_RELIEF_GAIN,
    _DEFAULT_HVE_SWEEP_BUDGET_SECONDS,
    _DEFAULT_HVE_SWEEP_MOVETIME_SECONDS,
)
from sturddle_view.engines import UCI_OPT_MULTIPV
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
_MULTIPV_MAX = 500
_PLAY, _ANALYSE, _ANALYSIS = "play", "analyse", "analysis"
# Not UCI_OPT_OWNBOOK's spelling: per-search options must use the engine's.
_OWNBOOK_SPELLING = "Ownbook"


class FakeAnalysis:
    """Mimics chess.engine.AnalysisResult where the driver touches it;
    `gate(self)` is awaited before the first info, standing in for search time."""

    def __init__(self, infos, best_move, gate=None):
        self._infos = list(infos)
        self._best = best_move
        self._gate = gate
        self.stopped = asyncio.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._gate is not None:
            gate, self._gate = self._gate, None
            await gate(self)
        if not self._infos:
            raise StopAsyncIteration
        return self._infos.pop(0)

    def stop(self):
        self.stopped.set()

    async def wait(self):
        return SimpleNamespace(move=self._best, ponder=None)


class BlindableEngine:
    """Duck-typed UciProtocol: analyse() = per-move sweep, analysis(multipv)
    = MultiPV sweep, analysis() = real search (best scripted move of
    root_moves), play() = the searchmoves probe."""

    def __init__(self, scores, *, honors_searchmoves=True, multipv_max=None, own_book=False):
        self.scores = dict(scores)
        self.honors = honors_searchmoves
        # No MultiPV option unless asked: the per-move sweep path.
        self.options = chess.engine.UciOptionMap()
        if multipv_max is not None:
            self.options[UCI_OPT_MULTIPV] = SimpleNamespace(max=multipv_max)
        if own_book:
            self.options[_OWNBOOK_SPELLING] = SimpleNamespace(name=_OWNBOOK_SPELLING)
        self.call_options: list[tuple[str, dict]] = []
        self.play_calls = 0
        self.sweep_calls: list[str] = []
        self.sweep_limits: list[float] = []
        self.multipv_calls: list[tuple[int, float]] = []
        self.multipv_infos = None  # scripted MultiPV output; None = ranked lines
        self.multipv_gate = None  # FakeAnalysis gate for the MultiPV search
        self.search_pools: list[list[str] | None] = []
        self.on_sweep = None  # hook fired per sweep search

    def _record(self, call, options):
        # Real python-chess raises on an option the engine lacks.
        assert all(name in self.options for name in options)
        self.call_options.append((call, options))

    async def play(self, board, limit, *, root_moves=None, options, **kw):
        self._record(_PLAY, options)
        self.play_calls += 1
        assert root_moves, "probe must restrict the search"
        if self.honors:
            return chess.engine.PlayResult(root_moves[0], None)
        other = next(m for m in board.legal_moves if m != root_moves[0])
        return chess.engine.PlayResult(other, None)

    async def analyse(self, board, limit, *, root_moves=None, options, **kw):
        self._record(_ANALYSE, options)
        if self.on_sweep is not None:
            self.on_sweep()
        assert root_moves is not None and len(root_moves) == 1
        mv = root_moves[0]
        self.sweep_calls.append(mv.uci())
        self.sweep_limits.append(limit.time)
        score = chess.engine.PovScore(
            chess.engine.Cp(self.scores[mv.uci()]), board.turn,
        )
        return {"score": score, "depth": 5}

    def _ranked_lines(self, board, multipv):
        """Top `multipv` lines, best first, like a real engine."""
        ranked = sorted(board.legal_moves, key=lambda m: -self.scores[m.uci()])
        return [
            {
                "score": chess.engine.PovScore(
                    chess.engine.Cp(self.scores[m.uci()]), board.turn,
                ),
                "pv": [m],
                "multipv": i + 1,
                "depth": 5,
            }
            for i, m in enumerate(ranked[:multipv])
        ]

    async def analysis(self, board, limit=None, *, root_moves=None, multipv=None, options, **kw):
        self._record(_ANALYSIS, options)
        if multipv is not None:
            assert root_moves is None
            if self.on_sweep is not None:
                self.on_sweep()
            self.multipv_calls.append((multipv, limit.time))
            infos = self.multipv_infos or self._ranked_lines(board, multipv)
            return FakeAnalysis(infos, None, self.multipv_gate)
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


def _make(
    scores=SCORES, *, level=5, honors=True, clock=None, multipv_max=None, own_book=False,
):
    settings = SimpleNamespace(hve_difficulty=level)
    hve = HumanVsEngine(engine_path="/fake/engine", bus=EventBus(), settings=settings)
    engine = BlindableEngine(
        scores, honors_searchmoves=honors, multipv_max=multipv_max, own_book=own_book,
    )
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
        hve_mod, "candidate_pool", lambda *a, **kw: (list(indices), 0.0),
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
    assert engine.sweep_limits == [_per_move_s()] * 4


def _per_move_s():
    n_moves = chess.Board(FOUR_MOVE_FEN).legal_moves.count()
    return max(_DEFAULT_HVE_SWEEP_MOVETIME_SECONDS, _DEFAULT_HVE_SWEEP_BUDGET_SECONDS / n_moves)


def _sweep_args(monkeypatch):
    """Capture the per-move scores candidate_pool receives."""
    seen = []

    def pool(scores, *a, **kw):
        seen.append(list(scores))
        return [0], 0.0

    monkeypatch.setattr(hve_mod, "candidate_pool", pool)
    return seen


def _legal_scores():
    return [SCORES[m.uci()] for m in chess.Board(FOUR_MOVE_FEN).legal_moves]


async def test_multipv_sweep_is_one_search(monkeypatch):
    legal = [m.uci() for m in chess.Board(FOUR_MOVE_FEN).legal_moves]
    # Ascending in legal order, so ranked lines arrive reversed.
    scores = {u: i for i, u in enumerate(legal)}
    hve, engine = _make(scores, multipv_max=_MULTIPV_MAX)
    seen = _sweep_args(monkeypatch)
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.multipv_calls == [(4, _per_move_s() * 4)]
    assert engine.sweep_calls == []
    assert seen == [[scores[u] for u in legal]]


async def test_multipv_max_below_moves_sweeps_the_rest(monkeypatch):
    hve, engine = _make(multipv_max=2)
    seen = _sweep_args(monkeypatch)
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.multipv_calls == [(2, _per_move_s() * 2)]
    assert sorted(engine.sweep_calls) == ["a2a3", "a2a4"]
    assert engine.sweep_limits == [_per_move_s()] * 2
    assert seen == [_legal_scores()]


async def test_multipv_knob_off_sweeps_per_move(monkeypatch):
    hve, engine = _make(multipv_max=_MULTIPV_MAX)
    hve._settings.hve_sweep_multipv = False
    _fix_pool(monkeypatch, [0])
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.multipv_calls == []
    assert len(engine.sweep_calls) == 4


async def test_multipv_sweep_holds_clock(monkeypatch):
    """Time spent inside the MultiPV search never shows on the clock."""
    initial_s, search_s = 60.0, 5.0
    fake = SimpleNamespace(t=0.0)
    clock = ChessClock(TimeControl(initial_seconds=initial_s), monotonic=lambda: fake.t)
    hve, engine = _make(clock=clock, multipv_max=_MULTIPV_MAX)
    monkeypatch.setattr(hve_mod, "_SWEEP_CLOCK_HOLD_SECONDS", 0)
    _fix_pool(monkeypatch, [0])
    _capture_commit(hve, monkeypatch)
    held = asyncio.get_running_loop().create_future()
    start_turn = clock.start_turn

    def rezero():
        start_turn()
        if fake.t and not held.done():
            held.set_result(None)

    clock.start_turn = rezero
    seen = []

    async def searching(analysis):
        fake.t += search_s
        await held
        seen.append(hve._remaining(chess.WHITE))

    engine.multipv_gate = searching
    clock.start_turn()
    await hve._think_and_play()
    assert seen == [initial_s]


async def test_own_book_off_below_max(monkeypatch):
    """Probe, sweep (MultiPV and per-move) and the restricted search all
    run with the engine's own book off: book replies carry no score and
    ignore searchmoves."""
    hve, engine = _make(multipv_max=2, own_book=True)
    _fix_pool(monkeypatch, [0])
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert {c for c, _ in engine.call_options} == {_PLAY, _ANALYSE, _ANALYSIS}
    assert all(opts == {_OWNBOOK_SPELLING: False} for _, opts in engine.call_options)


async def test_multipv_final_lines_keep_deepest_exact(monkeypatch):
    """Final line per slot; deeper duplicate wins in either order;
    dropped-out and bound moves sweep per move."""
    hve, engine = _make(multipv_max=_MULTIPV_MAX)
    seen = _sweep_args(monkeypatch)
    _capture_commit(hve, monkeypatch)

    def line(slot, uci, cp, depth, **flags):
        score = chess.engine.PovScore(chess.engine.Cp(cp), chess.WHITE)
        return {
            "multipv": slot, "pv": [chess.Move.from_uci(uci)], "score": score,
            "depth": depth, **flags,
        }

    engine.multipv_infos = [
        line(1, "a1b1", -999, 5),
        line(1, "a1b1", 0, 6),
        line(3, "a1b2", -999, 5),
        line(2, "a1b2", -30, 6),
        line(4, "a2a4", -999, 1),
        line(4, "a2a3", -999, 6, upperbound=True),
        line(5, "a1b1", -999, 5),
        {"multipv": 2, "depth": 7},
    ]
    await hve._think_and_play()
    assert sorted(engine.sweep_calls) == ["a2a3", "a2a4"]
    assert seen == [_legal_scores()]


async def test_own_book_untouched_on_degrade(monkeypatch):
    """searchmoves ignored: full strength, engine's own book stays on."""
    hve, engine = _make(honors=False, own_book=True)
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.call_options[-1] == (_ANALYSIS, {})


async def test_own_book_untouched_at_max(monkeypatch):
    hve, engine = _make(level=10, own_book=True)
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    assert engine.call_options == [(_ANALYSIS, {})]


async def test_relief_knobs_reach_candidate_pool(monkeypatch):
    """The settings knobs are passed through; relief derivation is
    candidate_pool's own (pure tests cover it)."""
    hve, _ = _make()
    seen = []
    monkeypatch.setattr(
        hve_mod, "candidate_pool",
        lambda *a, **kw: (seen.append(a), ([0], 0.0))[1],
    )
    _capture_commit(hve, monkeypatch)
    await hve._think_and_play()
    ((_, level, *_rest, gain, cap),) = seen
    assert level == 5
    assert (gain, cap) == (
        _DEFAULT_HVE_DEFICIT_RELIEF_GAIN, _DEFAULT_HVE_DEFICIT_RELIEF_CAP,
    )


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
    """Probe failure: notice published on every degraded move (client
    dedupes), no sweep, and the real search runs unrestricted."""
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
    assert len(toasts) == 2
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


async def test_stale_multipv_search_is_stopped(monkeypatch):
    """Takeback whose raw stop was lost: the sweep stops its own search,
    then neither falls back nor commits."""
    hve, engine = _make(multipv_max=2)
    monkeypatch.setattr(hve_mod, "_SWEEP_CLOCK_HOLD_SECONDS", 0)
    monkeypatch.setattr(
        hve, "_commit_engine_move", _forbidden("cancelled sweep must not commit"),
    )

    async def until_stopped(analysis):
        assert hve._analysis is analysis  # what _cancel_think stops
        hve._think_gen += 1
        await analysis.stopped.wait()

    engine.multipv_gate = until_stopped
    await hve._think_and_play()
    assert engine.sweep_calls == []
    assert hve._analysis is None


@pytest.mark.parametrize(("declared", "lines"), [(None, 4), (3, 3), (1, 0), (0, 0)])
async def test_sweep_multipv_lines(declared, lines):
    hve, engine = _make(multipv_max=_MULTIPV_MAX)
    engine.options[UCI_OPT_MULTIPV] = SimpleNamespace(max=declared)
    assert hve._sweep_multipv_lines(engine, 4) == lines


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
