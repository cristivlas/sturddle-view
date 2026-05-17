"""Characterization tests for HVE _board_event payloads -- no extraction yet.

Pins one JSON snapshot per mode (play, paused, viewing, editing, analyzing)
so that _board_event payload extraction (P11) can verify byte-comparable output.
Snapshots live in tests/fixtures/board_event_snapshots/.

Run with --snapshot-update to regenerate committed snapshots.
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl
from tests.conftest import SNAPSHOT_UPDATE_FLAG

SNAPSHOTS = pathlib.Path(__file__).parent / "fixtures" / "board_event_snapshots"

TC = TimeControl(300.0, 0.0)
# Even number of seed plies leaves white to move.
SEED_MOVES = ["e2e4", "e7e5"]
VIEW_MOVES = ["e2e4", "e7e5", "g1f3", "b8c6"]


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve():
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


def _stable_payload(event) -> dict:
    """Return payload with fields that vary per-run stripped out."""
    p = dict(event.payload)
    p.pop("game_id", None)
    # tablebase varies by whether syzygy path is set; normalise.
    if "tablebase" in p:
        p["tablebase"] = {"halfmove_clock": p["tablebase"].get("halfmove_clock")}
    return p


def _assert_snapshot(name: str, actual: dict, updating: bool) -> None:
    snap = SNAPSHOTS / name
    text = json.dumps(actual, indent=2, sort_keys=True) + "\n"
    if updating:
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        snap.write_text(text, encoding="utf-8")
        return
    assert snap.exists(), f"Snapshot missing: {snap} -- run with {SNAPSHOT_UPDATE_FLAG}"
    expected = json.loads(snap.read_text(encoding="utf-8"))
    assert actual == expected, f"Snapshot mismatch for {name}"


async def test_board_event_snapshot_play_mode_recorded(hve, request):
    updating = request.config.getoption(SNAPSHOT_UPDATE_FLAG, default=False)
    await hve.new_game(human_white=True, tc=TC, start_moves_uci=SEED_MOVES)

    event = hve._board_event()
    assert event.payload["view"] is None
    assert event.payload["editing"] is False
    assert event.payload["analyzing"] is False
    _assert_snapshot("play_mode.json", _stable_payload(event), updating)


async def test_board_event_snapshot_paused_recorded(hve, request):
    updating = request.config.getoption(SNAPSHOT_UPDATE_FLAG, default=False)
    await hve.new_game(human_white=True, tc=TC, start_moves_uci=SEED_MOVES)
    await hve.pause()

    event = hve._board_event()
    assert event.payload["view"] is None
    assert event.payload["analyzing"] is False
    # is_paused is the distinguishing field for this mode.
    assert hve.is_paused is True
    _assert_snapshot("paused_mode.json", _stable_payload(event), updating)


async def test_board_event_snapshot_viewing_at_cursor_recorded(hve, request):
    updating = request.config.getoption(SNAPSHOT_UPDATE_FLAG, default=False)
    moves_uci = [chess.Move.from_uci(u).uci() for u in VIEW_MOVES]
    await hve.enter_view_mode(
        start_fen=None,
        moves_uci=moves_uci,
        clock_history=None,
    )
    await hve.view_goto(2)

    event = hve._board_event()
    assert event.payload["view"] is not None
    assert event.payload["view"]["cursor"] == 2
    _assert_snapshot("viewing_at_cursor.json", _stable_payload(event), updating)


async def test_board_event_snapshot_editing_recorded(hve, request):
    updating = request.config.getoption(SNAPSHOT_UPDATE_FLAG, default=False)
    moves_uci = [chess.Move.from_uci(u).uci() for u in VIEW_MOVES]
    await hve.enter_view_mode(
        start_fen=None,
        moves_uci=moves_uci,
        clock_history=None,
    )
    await hve.enter_edit_mode()

    event = hve._board_event()
    assert event.payload["editing"] is True
    _assert_snapshot("editing_mode.json", _stable_payload(event), updating)


async def test_board_event_snapshot_analyzing_recorded(hve, request):
    updating = request.config.getoption(SNAPSHOT_UPDATE_FLAG, default=False)
    await hve.new_game(human_white=True, tc=TC, start_moves_uci=SEED_MOVES)
    await hve.pause()
    hve._run_analysis = AsyncMock()  # avoid needing a real engine subprocess
    await hve.start_analysis()

    event = hve._board_event()
    assert event.payload["analyzing"] is True
    _assert_snapshot("analyzing_mode.json", _stable_payload(event), updating)
