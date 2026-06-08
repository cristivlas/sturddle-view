"""Persistence of in-progress human-vs-engine games across server restarts."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import chess
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.events import EventBus
from sturddle_view.play.game_store import GameState, GameStore
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl
from sturddle_view.play.mode import Mode


# -------- GameStore: pure file I/O --------


def test_store_roundtrip(tmp_path):
    store = GameStore(path=tmp_path / "game.json")
    state = GameState(
        game_id="abc123",
        human_white=True,
        tc_initial_seconds=300.0,
        tc_increment_seconds=2.0,
        white_time=287.5,
        black_time=295.1,
        paused=False,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=[[300.0, 300.0], [298.0, 300.0], [298.0, 297.5]],
    )
    store.save(state)
    loaded = store.load()
    assert loaded == state


def test_store_load_missing_returns_none(tmp_path):
    store = GameStore(path=tmp_path / "absent.json")
    assert store.load() is None


def test_store_load_bad_json_returns_none(tmp_path):
    p = tmp_path / "game.json"
    p.write_text("{not json")
    store = GameStore(path=p)
    assert store.load() is None


def test_store_load_schema_mismatch_returns_none(tmp_path):
    p = tmp_path / "game.json"
    p.write_text('{"version": 999, "game_id": "x"}')
    store = GameStore(path=p)
    assert store.load() is None


def test_store_clear_removes_file(tmp_path):
    store = GameStore(path=tmp_path / "game.json")
    store.save(GameState(
        game_id="g", human_white=True, tc_initial_seconds=60.0,
        tc_increment_seconds=0.0, white_time=60.0, black_time=60.0, paused=False,
    ))
    assert store.path.exists()
    store.clear()
    assert not store.path.exists()
    # idempotent
    store.clear()


# -------- HumanVsEngine: persist on state changes --------


def _make_hve(tmp_path, engine_path="/fake/engine"):
    """A bare HVE with a real GameStore and event bus, no UCI subprocess."""
    store = GameStore(path=tmp_path / "current_game.json")
    hve = HumanVsEngine(
        engine_path=engine_path,
        bus=EventBus(),
        store=store,
    )
    # Bypass UCI: skip the popen_uci handshake and the engine reply task.
    fake_engine = MagicMock()
    fake_engine.send_line = MagicMock()
    async def _no_engine():
        return fake_engine
    hve._ensure_engine = _no_engine  # type: ignore[assignment]
    hve._engine_to_move = AsyncMock()
    return hve, store


async def test_persist_on_new_game(tmp_path):
    hve, store = _make_hve(tmp_path)
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    saved = store.load()
    assert saved is not None
    assert saved.human_white is True
    assert saved.tc_initial_seconds == 60.0
    assert saved.moves_uci == []


async def test_persist_on_human_move(tmp_path):
    hve, store = _make_hve(tmp_path)
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.submit_move("e2e4")
    saved = store.load()
    assert saved is not None
    assert saved.moves_uci == ["e2e4"]
    assert len(saved.clock_history) == 1


async def test_persist_on_pause_resume(tmp_path):
    hve, store = _make_hve(tmp_path)
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    await hve.pause()
    assert store.load().paused is True
    await hve.resume()
    assert store.load().paused is False


async def test_persist_on_takeback(tmp_path):
    """Take-back must rewrite the snapshot so a restart resumes the rolled-back position."""
    hve, store = _make_hve(tmp_path)
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    # Same simulated-engine-reply pattern as test_persist_after_engine_move.
    async def _fake_engine_reply():
        async with hve._lock:
            hve._clock.append_snapshot()
            hve._board.push(chess.Move.from_uci("e7e5"))
            hve._eval_history.append(None)
            await hve._persist()
    hve._engine_to_move = _fake_engine_reply
    await hve.submit_move("e2e4")
    assert store.load().moves_uci == ["e2e4", "e7e5"]
    await hve.takeback()
    saved = store.load()
    assert saved is not None
    assert saved.moves_uci == []
    assert saved.clock_history == []
    assert saved.paused is False


async def test_clear_on_resign(tmp_path):
    hve, store = _make_hve(tmp_path)
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    assert store.load() is not None
    await hve.resign()
    assert store.load() is None


async def test_clear_on_natural_game_over(tmp_path):
    """A mate (or any outcome reaching `_finalize_game_locked`) must clear
    both the disk snapshot AND in-memory state."""
    hve, store = _make_hve(tmp_path)
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    # Drive the board into Fool's Mate, with the engine reply mocked out so
    # _engine_to_move doesn't try to actually search.
    for uci in ["f2f3", "e7e5", "g2g4"]:
        # The first and third moves are White's (human); the second is Black's.
        # In our setup human is white, so the e7e5 needs to be applied directly.
        if hve._board.turn == chess.WHITE:
            await hve.submit_move(uci)
        else:
            hve._board.push(chess.Move.from_uci(uci))
            hve._eval_history.append(None)
    # White just played g2g4 — black to move with mate-in-1 available.
    hve._board.push(chess.Move.from_uci("d8h4"))
    hve._eval_history.append(None)
    assert hve._board.is_checkmate()
    async with hve._lock:
        hve._finalize_game_locked()
    assert store.load() is None
    assert hve._board is None
    assert hve._game_id is None


async def test_restart_mid_engine_think_resumes_with_engine_to_move(tmp_path):
    """Crash while the engine was searching: restored state shows the human's
    last move applied with the engine still to move (no orphan in-flight info).
    """
    hve, store = _make_hve(tmp_path)
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    # Engine "starts thinking" but never replies — simulating a crash mid-search.
    hve._engine_to_move = AsyncMock()
    await hve.submit_move("e2e4")
    saved = store.load()
    assert saved is not None
    assert saved.moves_uci == ["e2e4"]

    # Fresh HVE, fresh restore — like a server restart.
    fresh, _store2 = _make_hve(tmp_path)
    fresh.restore_from(saved)
    assert [m.uci() for m in fresh._board.move_stack] == ["e2e4"]
    assert fresh._board.turn == chess.BLACK  # engine's turn
    assert fresh._engine is None  # engine subprocess not spawned by restore
    assert fresh._think_task is None  # no orphan think task


async def test_persist_after_engine_move(tmp_path):
    """The engine reply path (`_think_and_play`) must persist post-move state."""
    hve, store = _make_hve(tmp_path)
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0))
    # Replace _engine_to_move with one that simulates an engine move via the
    # same code path: push a chosen move under the lock and call _persist.
    async def _fake_engine_reply():
        async with hve._lock:
            hve._clock.append_snapshot()
            hve._board.push(chess.Move.from_uci("e7e5"))
            hve._eval_history.append(None)
            await hve._persist()
    hve._engine_to_move = _fake_engine_reply
    await hve.submit_move("e2e4")
    saved = store.load()
    assert saved is not None
    assert saved.moves_uci == ["e2e4", "e7e5"]
    assert len(saved.clock_history) == 2


# -------- restore_from: rehydrate without spawning engine --------


def test_restore_from_replays_moves_and_clocks(tmp_path):
    hve, _store = _make_hve(tmp_path)
    state = GameState(
        game_id="restored",
        human_white=False,
        tc_initial_seconds=300.0,
        tc_increment_seconds=2.0,
        white_time=120.0,
        black_time=180.0,
        paused=True,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=[[300.0, 300.0], [298.0, 300.0], [298.0, 297.5]],
    )
    hve.restore_from(state)
    assert hve._game_id == "restored"
    assert hve._human_white is False
    assert hve._clock.white_time == 120.0
    assert hve._clock.black_time == 180.0
    assert hve.is_paused is True
    # Board reflects the three moves; black to move.
    assert hve._board.fullmove_number == 2
    assert hve._board.turn == chess.BLACK
    # Engine subprocess is not spawned by restore.
    assert hve._engine is None
    # Tick is deferred until first client subscribes.
    assert hve._tick_task is None
    assert hve._clock.turn_started_at is None


def test_restore_from_imported_position_with_start_fen(tmp_path):
    """Imported games store their start FEN; restore must replay onto it,
    not onto the standard starting position (which would crash because the
    moves aren't legal from startpos)."""
    hve, _store = _make_hve(tmp_path)
    # FEN with Black to move; engine reply Qd6→d1+ is only legal from this
    # position, not from startpos.
    start_fen = "1k1r4/pp1b1R2/3q2pp/4p3/2B5/4Q3/PPP2B2/2K5 b - - 0 1"
    state = GameState(
        game_id="imported",
        human_white=True,
        tc_initial_seconds=30.0,
        tc_increment_seconds=1.0,
        white_time=30.0,
        black_time=29.5,
        paused=False,
        moves_uci=["d6d1"],
        clock_history=[[30.0, 30.0]],
        start_fen=start_fen,
    )
    hve.restore_from(state)
    # Board reconstructed and engine's move applied.
    assert hve._board.move_stack[-1].uci() == "d6d1"
    assert hve._board.turn == chess.WHITE  # human's turn after Qd1+
    # Save round-trips the start_fen.
    asyncio.run(hve._persist())
    saved = _store.load()
    assert saved.start_fen == start_fen


async def test_republish_starts_tick_after_restore(tmp_path):
    """First republish_state on a restored unpaused game starts the tick."""
    hve, _store = _make_hve(tmp_path)
    hve.restore_from(GameState(
        game_id="r", human_white=True, tc_initial_seconds=60.0,
        tc_increment_seconds=0.0, white_time=58.0, black_time=60.0, paused=False,
        moves_uci=[], clock_history=[],
    ))
    assert hve._clock.turn_started_at is None
    await hve.republish_state()
    assert hve._clock.turn_started_at is not None
    assert hve._tick_task is not None
    await hve._cancel_tick()  # housekeeping


async def test_republish_kicks_engine_when_engine_to_move(tmp_path):
    """If a restored game has the engine to move, republish triggers the search."""
    hve, _store = _make_hve(tmp_path)
    # Human is black, so white (engine) is to move on the empty board.
    hve.restore_from(GameState(
        game_id="r", human_white=False, tc_initial_seconds=60.0,
        tc_increment_seconds=0.0, white_time=60.0, black_time=60.0, paused=False,
        moves_uci=[], clock_history=[],
    ))
    hve._engine_to_move = AsyncMock()
    await hve.republish_state()
    hve._engine_to_move.assert_awaited_once()
    await hve._cancel_tick()


# -------- _clock_running invariant --------


def _restored_unpaused(tmp_path):
    """A bare restored game in Mode.PLAY with the tick NOT started yet.
    Mirrors the state right after `restore_from` but before the first
    `republish_state` call."""
    hve, _store = _make_hve(tmp_path)
    hve.restore_from(GameState(
        game_id="r", human_white=True, tc_initial_seconds=60.0,
        tc_increment_seconds=0.0, white_time=60.0, black_time=60.0, paused=False,
        moves_uci=[], clock_history=[],
    ))
    return hve


def test_clock_running_no_board(tmp_path):
    """Fresh HVE has no board -- clock cannot run."""
    hve, _store = _make_hve(tmp_path)
    assert hve._board is None
    assert hve._clock_running is False


def test_clock_running_play(tmp_path):
    hve = _restored_unpaused(tmp_path)
    assert hve._mode is Mode.PLAY
    assert hve._clock_running is True


def test_clock_running_paused(tmp_path):
    hve = _restored_unpaused(tmp_path)
    hve._mode = Mode.PAUSED
    assert hve._clock_running is False


def test_clock_running_analyzing_from_play(tmp_path):
    hve = _restored_unpaused(tmp_path)
    hve._pre_analysis_mode = Mode.PLAY
    hve._mode = Mode.ANALYZING
    assert hve._clock_running is False


def test_clock_running_analyzing_from_paused(tmp_path):
    """The bug's exact predicate: user paused, then entered analysis.
    `_paused` is False (mode is ANALYZING), but the clock must NOT run."""
    hve = _restored_unpaused(tmp_path)
    hve._pre_analysis_mode = Mode.PAUSED
    hve._mode = Mode.ANALYZING
    assert hve._paused is False
    assert hve._analysis_mode is True
    assert hve._clock_running is False


def test_clock_running_viewing(tmp_path):
    hve = _restored_unpaused(tmp_path)
    hve._mode = Mode.VIEWING
    assert hve._clock_running is False


def test_clock_running_editing(tmp_path):
    hve = _restored_unpaused(tmp_path)
    hve._mode = Mode.EDITING
    assert hve._clock_running is False


def test_clock_running_game_over(tmp_path):
    """Fool's mate position -- board reports game over, clock must not run."""
    hve = _restored_unpaused(tmp_path)
    for uci in ("f2f3", "e7e5", "g2g4", "d8h4"):
        hve._board.push(chess.Move.from_uci(uci))
    assert hve._board.is_game_over()
    assert hve._clock_running is False


# -------- republish_state must respect analysis mode --------


async def test_republish_does_not_start_tick_when_analyzing(tmp_path):
    """Regression: a client refresh during paused+analysis must not start the
    clock. Pre-fix, `republish_state` only checked `_paused`; once the user
    pauses then enters analysis, mode flips to ANALYZING so `_paused` is False
    and the tick would start unintentionally."""
    hve = _restored_unpaused(tmp_path)
    # Simulate "user paused, then entered analysis" without spawning the real
    # analysis task (which would try to drive the UCI engine).
    hve._pre_analysis_mode = Mode.PAUSED
    hve._mode = Mode.ANALYZING
    assert hve._clock.turn_started_at is None
    await hve.republish_state()
    assert hve._clock.turn_started_at is None
    assert hve._tick_task is None


# -------- _clock_event.running tracks _clock_running --------


def test_clock_event_running_matches_clock_running(tmp_path):
    """_clock_event publishes `running` to clients -- it must agree with the
    same predicate republish_state uses. Verified across the live-play modes
    (VIEWING/EDITING take a different branch in _clock_event)."""
    hve = _restored_unpaused(tmp_path)
    for mode in (Mode.PLAY, Mode.PAUSED, Mode.ANALYZING):
        hve._mode = mode
        evt = hve._clock_event()
        assert evt.payload["running"] is hve._clock_running, mode


def _seed_state(store: GameStore):
    store.save(GameState(
        game_id="restored-on-boot",
        human_white=True,
        tc_initial_seconds=60.0,
        tc_increment_seconds=0.0,
        white_time=58.0,
        black_time=60.0,
        paused=False,
        moves_uci=["e2e4"],
        clock_history=[[60.0, 60.0]],
    ))


def test_lifespan_restores_when_engine_resolvable(tmp_path):
    fake_engine = tmp_path / "engine"
    fake_engine.write_text("")
    store = GameStore(path=tmp_path / "current_game.json")
    _seed_state(store)

    settings = Settings(token="t", auth_disabled=True)
    settings.engine_path = fake_engine
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry, game_store=store)
    with TestClient(app):
        assert app.state.hve is not None
        assert app.state.hve._game_id == "restored-on-boot"
        assert [m.uci() for m in app.state.hve._board.move_stack] == ["e2e4"]


def test_lifespan_skips_restore_when_no_engine(tmp_path):
    store = GameStore(path=tmp_path / "current_game.json")
    _seed_state(store)

    settings = Settings(token="t", auth_disabled=True)
    # No engine_path, no registry selection.
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry, game_store=store)
    with TestClient(app):
        assert app.state.hve is None
    # File is left in place so a later engine selection still picks it up.
    assert store.load() is not None


async def test_player_name_persisted_and_restored(tmp_path):
    """Regression: player_name must survive persist/restore (server restart)."""
    hve, store = _make_hve(tmp_path)
    await hve.new_game(human_white=True, tc=TimeControl(60.0, 0.0), player_name="Alice")
    await hve.submit_move("e2e4")
    saved = store.load()
    assert saved is not None
    assert saved.player_name == "Alice"

    fresh, _store2 = _make_hve(tmp_path)
    fresh.restore_from(saved)
    assert fresh._player_name == "Alice"


def test_player_name_store_roundtrip(tmp_path):
    """GameStore load/save round-trip preserves player_name; missing field defaults."""
    store = GameStore(path=tmp_path / "game.json")
    state = GameState(
        game_id="x", human_white=True, tc_initial_seconds=60.0,
        tc_increment_seconds=0.0, white_time=60.0, black_time=60.0, paused=False,
        player_name="Bob",
    )
    store.save(state)
    loaded = store.load()
    assert loaded.player_name == "Bob"

    # Old save without player_name field falls back to default.
    import json
    data = json.loads((tmp_path / "game.json").read_text())
    del data["player_name"]
    (tmp_path / "game.json").write_text(json.dumps(data))
    loaded2 = store.load()
    assert loaded2.player_name == "Human"
