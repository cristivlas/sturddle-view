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


async def test_per_move_autosave_writes_in_progress_pgn(hve):
    """A move during a live game writes a PGN with Result=*/unterminated."""
    h, _, tmp_path = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    pgns = list(tmp_path.glob("*.pgn"))
    assert len(pgns) == 1
    text = pgns[0].read_text()
    assert 'Result "*"' in text
    assert 'Termination "unterminated"' in text
    assert "1. e4" in text


async def test_resign_overwrites_in_progress_save(hve):
    """Resignation finalizes the same file the per-move autosave produced."""
    h, _, tmp_path = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    in_progress = list(tmp_path.glob("*.pgn"))
    assert len(in_progress) == 1
    in_progress_path = in_progress[0]

    await h.resign()
    final = list(tmp_path.glob("*.pgn"))
    assert len(final) == 1
    assert final[0] == in_progress_path  # same file, overwritten
    text = final[0].read_text()
    assert 'Result "0-1"' in text  # human white, resigned
    assert 'Termination "resignation"' in text


async def test_save_on_flag_fall(hve):
    """Time-flag must save the PGN before tearing down the game."""
    h, _, tmp_path = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    # _handle_flag_fall fires when the side-to-move's clock hits zero. Here
    # it's Black's turn (engine, mocked away), so Black is the loser.
    await h._handle_flag_fall()
    pgns = list(tmp_path.glob("*.pgn"))
    assert len(pgns) == 1
    text = pgns[0].read_text()
    assert 'Result "1-0"' in text  # Black flagged → White wins
    assert 'Termination "time_forfeit"' in text


async def test_pgn_includes_clk_annotations(hve):
    """Each ply gets a [%clk H:MM:SS] comment with monotonic decrease per side."""
    h, _, tmp_path = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    # White moves; clocks consume some time. With the engine mocked away,
    # we drive a second white "move" by directly emulating an engine reply
    # via the same path test_takeback uses.
    import asyncio as _asyncio

    await _asyncio.sleep(0.05)
    await h.submit_move("e2e4")
    # Inject Black's reply (engine mocked).
    async with h._lock:
        h._clock_history.append((h._white_time, h._black_time))
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("e7e5"))
    await h.resign()

    text = list(tmp_path.glob("*.pgn"))[0].read_text()
    # python-chess writes [%clk H:MM:SS] (no fractional part by default).
    assert "[%clk" in text
    # Two plies, two clk annotations.
    assert text.count("[%clk") == 2


async def test_pgn_includes_opening_header_for_known_line(hve):
    """A recognized opening writes ECO + Opening headers."""
    from sturddle_view.openings import OpeningBook

    h, _, tmp_path = hve
    h._openings = OpeningBook.load()  # default dir; loaded from vendored TSVs
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    # 1.e4 c5 — the opening book recognizes this as the Sicilian Defense.
    await h.submit_move("e2e4")
    async with h._lock:
        h._clock_history.append((h._white_time, h._black_time))
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("c7c5"))
    await h.resign()

    text = list(tmp_path.glob("*.pgn"))[0].read_text()
    assert "[ECO " in text
    assert "[Opening " in text
    assert "Sicilian" in text


async def test_pgn_no_opening_header_for_fen_imported_game(hve):
    """FEN-imported games can't be classified; no Opening header is written."""
    from sturddle_view.openings import OpeningBook

    h, _, tmp_path = hve
    h._openings = OpeningBook.load()
    # Seed a game from a non-startpos FEN — Sicilian after 2.Nf3.
    fen = "rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
    await h.new_game(human_white=True, tc=TimeControl(60, 0), start_fen=fen)
    async with h._lock:
        h._clock_history.append((h._white_time, h._black_time))
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("d7d6"))
    await h.resign()

    text = list(tmp_path.glob("*.pgn"))[0].read_text()
    assert "[ECO " not in text
    assert "[Opening " not in text


async def test_filename_stable_across_restore(hve, tmp_path):
    """A restored game keeps writing to the same PGN file."""
    h, settings, _ = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    original = list(tmp_path.glob("*.pgn"))[0]

    # Snapshot game state, simulate a fresh server, restore.
    bus = EventBus()
    h2 = HumanVsEngine(engine_path="/nonexistent", bus=bus, settings=settings)

    async def fake_ensure_engine():
        h2._engine = _StubEngine()
        return h2._engine

    h2._ensure_engine = fake_ensure_engine
    h2._engine_to_move = AsyncMock()

    # Build a GameState mirroring h's current state (h doesn't expose its
    # store path in this fixture, so reconstruct manually).
    from sturddle_view.play.game_store import GameState

    state = GameState(
        game_id=h._game_id,
        human_white=True,
        tc_initial_seconds=60.0,
        tc_increment_seconds=0.0,
        white_time=h._white_time,
        black_time=h._black_time,
        paused=False,
        moves_uci=["e2e4"],
        clock_history=[[w, b] for (w, b) in h._clock_history],
        start_fen=None,
        game_started_wall=h._game_started_wall,
    )
    h2.restore_from(state)
    await h2.resign()
    final = list(tmp_path.glob("*.pgn"))
    assert len(final) == 1
    assert final[0] == original  # same filename across the restart
