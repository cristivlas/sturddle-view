"""Play-mode capture and view-mode plumbing of per-ply eval_history."""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import (
    HumanVsEngine,
    TimeControl,
    ViewModeParams,
)


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


async def _engine_reply(h: HumanVsEngine, uci: str, score: dict | None = None) -> None:
    """Mimic _think_and_play's post-search portion."""
    move = chess.Move.from_uci(uci)
    async with h._lock:
        h._clock.history.append((h._clock.white_time, h._clock.black_time))
        h._consume_turn_time()
        h._board.push(move)
        h._eval_history.append(score)


async def test_new_game_starts_with_empty_eval_history(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    assert hve._eval_history == []


async def test_human_move_appends_none(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    assert hve._eval_history == [None]


async def test_engine_reply_records_score(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    await _engine_reply(hve, "e7e5", score={"cp": 15, "depth": 12})
    assert hve._eval_history == [None, {"cp": 15, "depth": 12}]


async def test_takeback_pops_eval_history(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    await _engine_reply(hve, "e7e5", score={"cp": 15})
    assert len(hve._eval_history) == 2
    await hve.takeback()
    assert hve._eval_history == []


async def test_play_game_snapshot_includes_eval_history(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    await _engine_reply(hve, "e7e5", score={"cp": 20, "depth": 8})
    start_fen, moves, clocks, w, b, evals = hve.play_game_snapshot()
    assert evals == [None, {"cp": 20, "depth": 8}]


async def test_enter_view_mode_populates_view_eval_history(hve):
    evals: list[dict | None] = [None, {"cp": 25, "depth": 10}]
    await hve.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        eval_history=evals,
    ))
    assert hve._view_eval_history == evals


async def test_play_to_view_transition_carries_evals(hve):
    """Simulates POST /game/view/start: snapshot -> ViewModeParams -> enter_view_mode."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    await _engine_reply(hve, "e7e5", score={"cp": 30, "depth": 11})
    start_fen, moves, clocks, w, b, evals = hve.play_game_snapshot()
    pre_transition = list(evals)
    await hve.enter_view_mode(ViewModeParams(
        start_fen=start_fen,
        moves_uci=moves,
        clock_history=clocks or None,
        final_white_time=w,
        final_black_time=b,
        eval_history=evals,
    ))
    assert hve._view_eval_history == pre_transition


async def test_pgn_text_play_mode_includes_evals(hve):
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    await _engine_reply(hve, "e7e5", score={"cp": 30, "depth": 11})
    result = hve.get_pgn_text()
    assert result is not None
    text, _ = result
    # Cutechess token must appear (sign-flipped on black-to-move ply).
    assert "-0.30/11" in text or "-0.30 " in text
    # %clk must be dropped when evals are present.
    assert "%clk" not in text


async def test_eval_history_persists_across_restore(tmp_path):
    """A restart must not erase engine evals captured before the restart."""
    from sturddle_view.play.game_store import GameStore

    bus = EventBus()
    store = GameStore(tmp_path / "current_game.json")
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, store=store)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()

    await h.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await h.submit_move("e2e4")
    await _engine_reply(h, "e7e5", score={"cp": 42, "depth": 14})
    # _engine_reply doesn't persist (mimics the post-search portion only);
    # trigger an explicit persist so the snapshot includes the engine ply.
    async with h._lock:
        await h._persist()

    saved = store.load()
    assert saved is not None
    assert saved.eval_history == [None, {"cp": 42, "depth": 14}]

    fresh = HumanVsEngine(engine_path="/nonexistent", bus=bus, store=store)
    fresh.restore_from(saved)
    assert fresh._eval_history == [None, {"cp": 42, "depth": 14}]


async def test_restore_from_old_save_falls_back_to_nones(tmp_path):
    """Saves that predate eval_history persistence rehydrate cleanly."""
    from sturddle_view.play.game_store import GameState

    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus)
    # Construct a GameState by hand with empty eval_history but non-empty
    # moves_uci (length mismatch -> fall back).
    state = GameState(
        game_id="g1",
        human_white=True,
        tc_initial_seconds=60.0,
        tc_increment_seconds=0.0,
        white_time=58.0,
        black_time=59.0,
        paused=False,
        moves_uci=["e2e4", "e7e5"],
        clock_history=[[60.0, 60.0], [58.0, 60.0]],
        eval_history=[],
        start_fen=None,
    )
    h.restore_from(state)
    assert h._eval_history == [None, None]


async def test_play_from_here_seeds_eval_prefix(hve):
    """Forking mid-game keeps the view game's evals for the seeded plies."""
    evals: list[dict | None] = [{"cp": 10}, {"cp": -5}, {"cp": 30}, {"cp": -20}]
    await hve.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3", "b8c6"],
        clock_history=None,
        eval_history=evals,
    ))
    await hve.view_last()
    await hve.view_back()  # cursor at ply 3 (after Nf3)
    assert hve._view_cursor == 3
    await hve.play_from_here(tc=TimeControl(60.0, 0.0))
    assert hve._eval_history == evals[:3]


async def test_play_from_here_without_view_evals_seeds_nones(hve):
    """A view game with no evals forks into an all-None eval history."""
    await hve.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
    ))
    await hve.view_last()
    await hve.play_from_here(tc=TimeControl(60.0, 0.0))
    assert hve._eval_history == [None, None]


async def test_forked_eval_history_persists_across_restore(tmp_path):
    """The seeded prefix survives the autosave/restore round-trip."""
    from sturddle_view.play.game_store import GameStore

    bus = EventBus()
    store = GameStore(tmp_path / "current_game.json")
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, store=store)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()

    evals: list[dict | None] = [{"cp": 12, "depth": 9}, None, {"cp": -7, "depth": 11}]
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
        eval_history=evals,
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60.0, 0.0))

    saved = store.load()
    assert saved is not None
    assert saved.eval_history == evals

    fresh = HumanVsEngine(engine_path="/nonexistent", bus=bus, store=store)
    fresh.restore_from(saved)
    assert fresh._eval_history == evals


async def test_pgn_text_play_mode_never_emits_clk(hve):
    """Play-mode export always uses cutechess tokens, even when no engine
    has moved (human ply -> time-only token, still no %clk)."""
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    result = hve.get_pgn_text()
    assert result is not None
    text, _ = result
    assert "%clk" not in text
