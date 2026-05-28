"""Server-side gate: start_analysis skips the engine search task when
`settings.ai_enabled` is on.

The AI agent owns engine use via tool calls, so running a second
go-infinite in parallel would contend for CPU. The mode/state machinery
still flips identically -- only the engine task is suppressed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl
from sturddle_view.play.mode import Mode


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


def _build_hve(*, ai_enabled: bool, tmp_path) -> HumanVsEngine:
    settings = Settings()
    settings.pgn_autosave = False
    settings.pgn_dir = tmp_path
    settings.ai_enabled = ai_enabled
    h = HumanVsEngine(engine_path="/nonexistent", bus=EventBus(), settings=settings)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    h._run_analysis = AsyncMock()
    return h


async def _start_paused_game(h: HumanVsEngine) -> None:
    await h.new_game(
        human_white=True,
        tc=TimeControl(initial_seconds=60.0, increment_seconds=0.0),
    )
    await h.pause()


@pytest.mark.asyncio
async def test_start_analysis_launches_engine_when_ai_disabled(tmp_path):
    h = _build_hve(ai_enabled=False, tmp_path=tmp_path)
    await _start_paused_game(h)

    await h.start_analysis()

    assert h._mode is Mode.ANALYZING
    h._run_analysis.assert_called_once()
    assert h._analysis_task is not None


@pytest.mark.asyncio
async def test_start_analysis_skips_engine_when_ai_enabled(tmp_path):
    h = _build_hve(ai_enabled=True, tmp_path=tmp_path)
    await _start_paused_game(h)

    await h.start_analysis()

    # Mode still flips so the client UI uniformly observes ANALYZING --
    # the only difference is no engine task fires.
    assert h._mode is Mode.ANALYZING
    h._run_analysis.assert_not_called()
    assert h._analysis_task is None


@pytest.mark.asyncio
async def test_stop_analysis_is_safe_when_no_engine_was_started(tmp_path):
    # If start skipped the engine, stop must still cleanly exit analysis
    # mode -- _cancel_analysis is a no-op when _analysis_task is None.
    h = _build_hve(ai_enabled=True, tmp_path=tmp_path)
    await _start_paused_game(h)
    await h.start_analysis()
    assert h._mode is Mode.ANALYZING

    await h.stop_analysis()

    assert h._mode is not Mode.ANALYZING
