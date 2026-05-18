"""Characterization tests for HVE PGN autosave output -- no extraction yet.

Pins byte-comparable PGN output so that pgn_build.py (P5) can verify
the extracted producer matches. Snapshots live in
tests/fixtures/pgn_autosave_snapshots/.

Run with --snapshot-update to regenerate committed snapshots.
"""
from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock

import pytest

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl
from tests.conftest import SNAPSHOT_UPDATE_FLAG

SNAPSHOTS = pathlib.Path(__file__).parent / "fixtures" / "pgn_autosave_snapshots"

TC = TimeControl(300.0, 2.0)

# Even number of seed plies leaves white to move, so submit_move (human=white) is valid.
STARTPOS_SEED = ["e2e4", "e7e5"]
STARTPOS_HUMAN_MOVE = "g1f3"

CUSTOM_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"
CUSTOM_FEN_SEED = ["g1f3", "b8c6"]
CUSTOM_FEN_HUMAN_MOVE = "f1c4"

SEEDED_CLOCK_SEED = ["e2e4", "e7e5", "g1f3", "b8c6"]
SEEDED_CLOCK_HISTORY = [
    (300.0, 300.0),
    (300.0, 298.0),
    (297.0, 298.0),
    (297.0, 295.0),
]
SEEDED_CLOCK_HUMAN_MOVE = "f1c4"


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve(tmp_path):
    settings = Settings()
    settings.pgn_autosave = True
    settings.pgn_dir = str(tmp_path)
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, settings=settings)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h, tmp_path


def _read_pgn(path: pathlib.Path) -> str:
    """Return PGN text with run-varying headers stripped."""
    lines = path.read_text(encoding="utf-8").splitlines()
    skip = {"[Date ", "[White \"", "[Black \""}
    stable = [ln for ln in lines if not any(ln.startswith(p) for p in skip)]
    return "\n".join(stable)


def _assert_snapshot(name: str, actual: str, updating: bool) -> None:
    snap = SNAPSHOTS / name
    if updating:
        SNAPSHOTS.mkdir(parents=True, exist_ok=True)
        snap.write_text(actual, encoding="utf-8")
        return
    assert snap.exists(), f"Snapshot missing: {snap} -- run with {SNAPSHOT_UPDATE_FLAG}"
    expected = snap.read_text(encoding="utf-8")
    assert actual == expected, f"Snapshot mismatch for {name}"


async def test_autosave_startpos_short_game_matches_snapshot(hve, request):
    h, tmp_path = hve
    updating = request.config.getoption(SNAPSHOT_UPDATE_FLAG, default=False)

    await h.new_game(human_white=True, tc=TC, start_moves_uci=STARTPOS_SEED)
    await h.submit_move(STARTPOS_HUMAN_MOVE)
    await h.resign()

    pgns = sorted(tmp_path.glob("*.pgn"))
    assert len(pgns) == 1
    actual = _read_pgn(pgns[0])
    _assert_snapshot("startpos_short_game.pgn", actual, updating)


async def test_autosave_from_custom_fen_matches_snapshot(hve, request):
    h, tmp_path = hve
    updating = request.config.getoption(SNAPSHOT_UPDATE_FLAG, default=False)

    await h.new_game(
        human_white=True, tc=TC,
        start_fen=CUSTOM_FEN,
        start_moves_uci=CUSTOM_FEN_SEED,
    )
    await h.submit_move(CUSTOM_FEN_HUMAN_MOVE)
    await h.resign()

    pgns = sorted(tmp_path.glob("*.pgn"))
    assert len(pgns) == 1
    actual = _read_pgn(pgns[0])
    _assert_snapshot("custom_fen_game.pgn", actual, updating)


async def test_autosave_with_seeded_clock_history_matches_snapshot(hve, request):
    h, tmp_path = hve
    updating = request.config.getoption(SNAPSHOT_UPDATE_FLAG, default=False)

    await h.new_game(
        human_white=True,
        tc=TC,
        start_moves_uci=SEEDED_CLOCK_SEED,
        seed_clock_history=SEEDED_CLOCK_HISTORY,
        seed_final_white_time=297.0,
        seed_final_black_time=295.0,
    )
    await h.submit_move(SEEDED_CLOCK_HUMAN_MOVE)
    await h.resign()

    pgns = sorted(tmp_path.glob("*.pgn"))
    assert len(pgns) == 1
    actual = _read_pgn(pgns[0])
    _assert_snapshot("seeded_clock_history_game.pgn", actual, updating)
