"""PGN save behavior."""
from __future__ import annotations

from pathlib import Path

import chess
import pytest
from unittest.mock import AsyncMock

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve(tmp_path):
    settings = Settings()
    settings.pgn_autosave = True
    settings.pgn_dir = tmp_path
    settings.tc_initial_seconds = 300
    settings.tc_increment_seconds = 0
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, settings=settings)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h, settings, tmp_path


async def test_save_on_resign(hve):
    h, _, tmp_path = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    await h.resign()
    pgns = list(tmp_path.glob("*.pgn"))
    assert len(pgns) == 1
    text = pgns[0].read_text()
    assert "[White \"Human\"]" in text
    assert "1. e4" in text
    assert "[Termination \"resignation\"]" in text


async def test_no_save_when_disabled(tmp_path):
    settings = Settings()
    settings.pgn_autosave = False
    settings.pgn_dir = tmp_path
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, settings=settings)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()

    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    await h.resign()
    assert list(tmp_path.glob("*.pgn")) == []


async def test_no_save_for_empty_game(hve):
    h, _, tmp_path = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.resign()
    assert list(tmp_path.glob("*.pgn")) == []


async def test_filename_unique_across_games(hve):
    h, _, tmp_path = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    await h.resign()
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("d2d4")
    await h.resign()
    assert len(list(tmp_path.glob("*.pgn"))) == 2
