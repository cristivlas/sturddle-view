"""HVE._eval_pov resolution across modes.

Covers the four code paths -- settings=None, 'white', 'engine', 'human' --
because the function is exercised only at runtime (engine_info pump
serialization) and a typo there silently kills PV/arrow rendering."""
from __future__ import annotations

import chess

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine


def _make_hve(*, play_eval_pov=None, human_white=True) -> HumanVsEngine:
    kwargs = {"token": "t", "auth_disabled": True}
    if play_eval_pov is not None:
        kwargs["play_eval_pov"] = play_eval_pov
    h = HumanVsEngine(
        engine_path="/nonexistent",
        bus=EventBus(),
        settings=Settings(**kwargs),
    )
    h._human_white = human_white
    return h


def test_eval_pov_settings_none_defaults_to_white():
    h = HumanVsEngine(engine_path="/nonexistent", bus=EventBus(), settings=None)
    assert h._eval_pov(chess.WHITE) == chess.WHITE
    assert h._eval_pov(chess.BLACK) == chess.WHITE


def test_eval_pov_explicit_white_returns_white():
    h = _make_hve(play_eval_pov="white")
    assert h._eval_pov(chess.WHITE) == chess.WHITE
    assert h._eval_pov(chess.BLACK) == chess.WHITE


def test_eval_pov_engine_mode_returns_stm():
    h = _make_hve(play_eval_pov="engine")
    assert h._eval_pov(chess.WHITE) == chess.WHITE
    assert h._eval_pov(chess.BLACK) == chess.BLACK


def test_eval_pov_human_mode_white_player():
    h = _make_hve(play_eval_pov="human", human_white=True)
    assert h._eval_pov(chess.WHITE) == chess.WHITE
    assert h._eval_pov(chess.BLACK) == chess.WHITE


def test_eval_pov_human_mode_black_player():
    h = _make_hve(play_eval_pov="human", human_white=False)
    assert h._eval_pov(chess.WHITE) == chess.BLACK
    assert h._eval_pov(chess.BLACK) == chess.BLACK
