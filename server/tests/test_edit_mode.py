"""Server-authoritative board edit mode: state transitions and that
play/analysis/view-nav endpoints reject while editing."""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import pytest

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
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, settings=settings)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


async def _enter_view(h: HumanVsEngine, fen: str | None = None) -> None:
    await h.enter_view_mode(
        start_fen=fen,
        moves_uci=[],
        clock_history=None,
    )


async def test_enter_edit_mode_requires_view_mode(hve: HumanVsEngine):
    with pytest.raises(RuntimeError, match="enter view mode"):
        await hve.enter_edit_mode()


async def test_enter_edit_mode_snapshots_current_fen(hve: HumanVsEngine):
    await _enter_view(hve)
    pre = await hve.enter_edit_mode()
    assert hve._editing is True
    assert pre == chess.STARTING_FEN
    assert hve._edit_pre_fen == chess.STARTING_FEN


async def test_commit_edit_with_valid_fen_enters_view_at_fen(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    new_fen = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"
    await hve.commit_edit(new_fen)
    assert hve._editing is False
    assert hve._viewing is True
    assert hve._board.fen() == new_fen


async def test_commit_edit_with_illegal_position_stays_editing(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    # Two white kings -> illegal.
    bad = "4k3/8/8/8/8/8/8/3KK3 w - - 0 1"
    with pytest.raises(RuntimeError):
        await hve.commit_edit(bad)
    assert hve._editing is True


async def test_commit_edit_with_invalid_fen_stays_editing(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(RuntimeError, match="invalid FEN"):
        await hve.commit_edit("not a fen")
    assert hve._editing is True


async def test_cancel_edit_restores_pre_edit_fen(hve: HumanVsEngine):
    start = "4k3/8/8/8/8/8/8/4K3 w - - 0 1"
    await _enter_view(hve, fen=start)
    await hve.enter_edit_mode()
    await hve.cancel_edit()
    assert hve._editing is False
    assert hve._board.fen() == start


async def test_editing_blocks_view_nav(hve: HumanVsEngine):
    await hve.enter_view_mode(
        start_fen=None, moves_uci=["e2e4", "e7e5"], clock_history=None,
    )
    await hve.enter_edit_mode()
    with pytest.raises(RuntimeError, match="edit mode is on"):
        await hve.view_first()
    with pytest.raises(RuntimeError, match="edit mode is on"):
        await hve.view_back()
    with pytest.raises(RuntimeError, match="edit mode is on"):
        await hve.view_forward()
    with pytest.raises(RuntimeError, match="edit mode is on"):
        await hve.view_last()
    with pytest.raises(RuntimeError, match="edit mode is on"):
        await hve.view_goto(0)


async def test_editing_blocks_play_from_here(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(RuntimeError, match="edit mode is on"):
        await hve.play_from_here(tc=TimeControl(initial_seconds=60.0, increment_seconds=0.0))


async def test_editing_blocks_analysis(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(RuntimeError, match="edit mode is on"):
        await hve.start_analysis()


async def test_editing_blocks_import(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(RuntimeError, match="edit mode is on"):
        await hve.enter_view_mode(start_fen=None, moves_uci=[], clock_history=None)


async def test_editing_blocks_new_game(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(RuntimeError, match="edit mode is on"):
        await hve.new_game(
            human_white=True,
            tc=TimeControl(initial_seconds=60.0, increment_seconds=0.0),
        )


async def test_enter_edit_mode_stops_analysis(hve: HumanVsEngine):
    """Entering edit mode while analyzing must cancel analysis."""
    await _enter_view(hve)
    # Fake analysis active without running the engine task.
    hve._analysis_mode = True
    cancel_called = []

    async def fake_cancel():
        cancel_called.append(True)
        hve._analysis_mode = False

    hve._cancel_analysis = fake_cancel
    await hve.enter_edit_mode()
    assert hve._editing is True
    assert hve._analysis_mode is False
    assert cancel_called == [True]


async def test_enter_edit_mode_rejects_when_already_editing(hve: HumanVsEngine):
    await _enter_view(hve)
    await hve.enter_edit_mode()
    with pytest.raises(RuntimeError, match="already in edit mode"):
        await hve.enter_edit_mode()


async def test_board_update_payload_includes_editing_flag(hve: HumanVsEngine):
    q = await hve._bus.subscribe()
    await _enter_view(hve)
    await hve.enter_edit_mode()
    # Drain the queue and grab the most recent board_update.
    latest = None
    while not q.empty():
        evt = q.get_nowait()
        if evt.kind == "board_update":
            latest = evt
    assert latest is not None
    assert latest.payload.get("editing") is True
