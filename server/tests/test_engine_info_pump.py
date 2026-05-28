"""Deterministic mid-search cancel via the pump's `first_info_event`
hook. Long-search fake holds the search open until UCI `stop`."""
from __future__ import annotations

import asyncio
from pathlib import Path

import chess
import chess.engine
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.engine_info_pump import pump_engine_info

from .conftest import make_long_search_fake_uci


@pytest.mark.asyncio
async def test_pump_cancel_after_first_info_returns_cancelled(tmp_path: Path):
    engine_path = make_long_search_fake_uci(
        tmp_path, "midcancel_fake",
        bestmove="e2e4", pv="e2e4", score_cp=42,
    )
    transport, engine = await chess.engine.popen_uci(engine_path)
    try:
        board = chess.Board()
        token = CancelToken()
        first_info = asyncio.Event()

        async def _flip_on_first_info():
            await first_info.wait()
            token.cancel()

        with await engine.analysis(board, limit=chess.engine.Limit(depth=99)) as analysis:
            stop_calls = []
            real_stop = analysis.stop
            analysis.stop = lambda: (stop_calls.append(True), real_stop())[1]
            watcher = asyncio.create_task(_flip_on_first_info())
            try:
                last_info, cancelled = await pump_engine_info(
                    analysis,
                    bus=EventBus(),
                    game_id="g",
                    board=board,
                    pov=chess.WHITE,
                    cancel_token=token,
                    first_info_event=first_info,
                )
            finally:
                watcher.cancel()

        assert cancelled is True
        assert "score" in last_info
        assert stop_calls, "pump must call analysis.stop() on cancel"
    finally:
        await engine.quit()
        transport.close()


