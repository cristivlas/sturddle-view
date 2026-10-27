"""Engine-only analysis requests MultiPV lines (SV_ANALYSIS_MULTIPV, capped
by the engine's MultiPV max; single-line when the engine has no MultiPV),
and the /game/sync replay cache keeps one payload per line, best first."""
from __future__ import annotations

import chess
import chess.engine
import pytest

from sturddle_view.config import _DEFAULT_ANALYSIS_MULTIPV, Settings
from sturddle_view.engines import UCI_OPT_MULTIPV
from sturddle_view.events import EVT_ENGINE_INFO, EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.play.mode import Mode

ENGINE_MAX_LINES = 2
# Well above any configured line count.
ENGINE_ROOMY_MAX_LINES = 500
GAME_ID = "g1"


def _multipv_option(max_lines: int) -> chess.engine.Option:
    return chess.engine.Option(UCI_OPT_MULTIPV, "spin", 1, 1, max_lines, [])


def _info(rank: int, depth: int, move: str) -> chess.engine.InfoDict:
    return {
        "multipv": rank, "depth": depth,
        "score": chess.engine.PovScore(chess.engine.Cp(rank), chess.WHITE),
        "pv": [chess.Move.from_uci(move)],
    }


class _FakeAnalysis:
    """Async-iterable analysis that yields canned infos, then ends."""

    def __init__(self, infos):
        self._infos = list(infos)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._infos:
            raise StopAsyncIteration
        return self._infos.pop(0)

    async def wait(self):
        return chess.engine.BestMove(None, None)

    def stop(self):
        pass


class _FakeEngine:
    def __init__(self, options, infos=()):
        self.options = options
        self.infos = infos
        self.multipv = "unset"

    async def analysis(self, board, multipv=None):
        self.multipv = multipv
        return _FakeAnalysis(self.infos)


def _hve(monkeypatch, engine, *, analysis_multipv=None) -> tuple[HumanVsEngine, list]:
    settings = Settings()
    if analysis_multipv is not None:
        settings.analysis_multipv = analysis_multipv
    h = HumanVsEngine(engine_path="/p/engine", bus=EventBus(), settings=settings)
    h._board = chess.Board()
    h._game_id = GAME_ID
    h._mode = Mode.ANALYZING
    published: list = []

    async def _cap(ev):
        published.append(ev)
    h._bus.publish = _cap

    async def _cleanup():
        pass

    async def _fake_spawn(_sup, _settings):
        return engine, _cleanup
    monkeypatch.setattr("sturddle_view.play.human_vs_engine.make_analysis_supervisor",
                        lambda *_a: None)
    monkeypatch.setattr("sturddle_view.play.human_vs_engine.spawn_analysis_engine", _fake_spawn)
    return h, published


@pytest.mark.parametrize(("options", "setting", "want"), [
    ({UCI_OPT_MULTIPV: _multipv_option(ENGINE_ROOMY_MAX_LINES)}, None, _DEFAULT_ANALYSIS_MULTIPV),
    ({UCI_OPT_MULTIPV: _multipv_option(ENGINE_MAX_LINES)}, None, ENGINE_MAX_LINES),
    ({}, None, None),  # no MultiPV option: plain single-line search
    ({UCI_OPT_MULTIPV: _multipv_option(ENGINE_ROOMY_MAX_LINES)}, 1, None),  # setting disables
])
async def test_run_analysis_requests_capped_multipv(monkeypatch, options, setting, want):
    engine = _FakeEngine(options)
    h, _ = _hve(monkeypatch, engine, analysis_multipv=setting)
    await h._run_analysis(GAME_ID, chess.Board())
    assert engine.multipv == want


async def test_replay_cache_keeps_latest_payload_per_line_best_first(monkeypatch):
    infos = [_info(1, 1, "e2e4"), _info(2, 1, "d2d4"), _info(1, 2, "g1f3"), _info(2, 2, "c2c4")]
    engine = _FakeEngine({UCI_OPT_MULTIPV: _multipv_option(ENGINE_MAX_LINES)}, infos)
    h, published = _hve(monkeypatch, engine)
    await h._run_analysis(GAME_ID, chess.Board())

    published.clear()
    await h._republish_last_analysis_info()
    replay = [(ev.payload["multipv"], ev.payload["pv_uci"][0])
              for ev in published if ev.kind == EVT_ENGINE_INFO]
    assert replay == [(1, "g1f3"), (2, "c2c4")]
