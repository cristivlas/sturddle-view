"""Human vs engine driver, on top of python-chess's async UCI interface.

Skips the tournament manager and proxy entirely. Streams engine info (depth,
score, PV, NPS), clock ticks, and game state onto the same event bus the
tournament path uses.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.engine
import chess.pgn

from .._atomic import atomic_write_text
from ..events import Event, EventBus
from .game_store import GameState, GameStore

log = logging.getLogger(__name__)

CLOCK_TICK_INTERVAL = 0.25  # seconds


@dataclass
class TimeControl:
    initial_seconds: float
    increment_seconds: float = 0.0


def _serialize_info(
    info: chess.engine.InfoDict, board: chess.Board,
    eval_pov: chess.Color = chess.WHITE,
) -> dict:
    out: dict = {}
    if "depth" in info:
        out["depth"] = info["depth"]
    if "seldepth" in info:
        out["seldepth"] = info["seldepth"]
    if "nodes" in info:
        out["nodes"] = info["nodes"]
    if "nps" in info:
        out["nps"] = info["nps"]
    if "tbhits" in info:
        out["tbhits"] = info["tbhits"]
    if "hashfull" in info:
        out["hashfull"] = info["hashfull"]
    if "time" in info:
        out["time"] = info["time"]
    score = info.get("score")
    if score is not None:
        pov = score.pov(eval_pov)
        if pov.is_mate():
            out["score"] = {"mate": pov.mate()}
        else:
            out["score"] = {"cp": pov.score()}
    pv = info.get("pv")
    if pv:
        try:
            out["pv"] = [board.variation_san(pv)]
        except (ValueError, AssertionError):
            out["pv"] = [m.uci() for m in pv]
        out["pv_uci"] = [m.uci() for m in pv]
    return out


def _moves_san(board: chess.Board, start_fen: str | None = None) -> list[str]:
    """Return the current move stack as SAN strings, replayed from start_fen
    (or the standard starting position when None)."""
    if not board.move_stack:
        return []
    replay = chess.Board(start_fen) if start_fen else chess.Board()
    out = []
    for m in board.move_stack:
        out.append(replay.san(m))
        replay.push(m)
    return out


class HumanVsEngine:
    """Single-game driver. Holds one active game at a time."""

    def __init__(
        self,
        engine_path: str,
        bus: EventBus,
        openings=None,
        settings=None,
        store: GameStore | None = None,
    ) -> None:
        self._engine_path = engine_path
        self._bus = bus
        self._openings = openings  # Optional[OpeningBook]
        self._settings = settings  # Optional[Settings]
        self._store = store
        self._engine: chess.engine.UciProtocol | None = None
        # Display name shown to the user. Caller may set via set_engine_name()
        # to override (e.g. with the registry name). Otherwise _ensure_engine
        # fills it from the engine's UCI `id name`, falling back to basename.
        self._engine_name: str | None = None
        # User-overridden UCI options applied at engine launch via setoption.
        # Updated by the API layer on every fetch from the registry; takes
        # effect on the next launch (existing process keeps its options).
        self._engine_options: dict = {}
        # Extra command-line args / per-engine env. Updated by the API
        # layer on every fetch; take effect on the next launch.
        self._engine_args: list[str] = []
        self._engine_env: dict[str, str] = {}
        self._board: chess.Board | None = None
        # FEN of the board *before* any moves on _board.move_stack — None for
        # games that began at startpos. Persisted so restore_from can rebuild
        # an imported game whose move_stack isn't replayable from startpos.
        self._start_fen: str | None = None
        self._game_id: str | None = None
        # Wall-clock time the current game started, used for stable PGN
        # filenames across per-move autosaves and end-of-game finalization.
        self._game_started_wall: float | None = None
        self._human_white: bool = True
        self._tc: TimeControl = TimeControl(300.0, 0.0)
        self._white_time: float = 0.0
        self._black_time: float = 0.0
        # Wall-clock timestamp when the side to move started thinking.
        # Used to compute remaining time on each tick.
        self._turn_started_at: float | None = None
        # Per-ply snapshot of (white_time, black_time) BEFORE the move at that
        # ply was played. Used to restore clocks on take-back.
        # Index = ply number (length of move_stack).
        self._clock_history: list[tuple[float, float]] = []
        self._think_task: asyncio.Task | None = None
        self._analysis = None  # active chess.engine.AnalysisResult, if any
        self._think_gen: int = 0  # search generation; bumped on cancel
        self._tick_task: asyncio.Task | None = None
        # Pause is only allowed on the human's turn (engine is idle then).
        # While paused, the tick loop is stopped and submit_move is rejected.
        self._paused: bool = False
        # Analysis mode (UCI go infinite): clocks frozen, board read-only,
        # engine streams info on the current position. Mutually exclusive
        # with normal play; toggled via start_analysis/stop_analysis.
        self._analysis_mode: bool = False
        self._analysis_task: asyncio.Task | None = None
        # View mode: cursor-based playback of an imported / loaded game.
        # _board is rebuilt from _view_full_moves[:_view_cursor] on every
        # navigation, so analyze sees the right position automatically.
        # Autosave, submit_move, engine thinking, and clocks are all gated
        # off while viewing. Exits via play_from_here.
        self._viewing: bool = False
        self._view_cursor: int = 0  # 0..len(_view_full_moves) inclusive
        self._view_full_moves: list[chess.Move] = []
        # Per-ply pre-move (white, black) snapshots from the imported PGN's
        # [%clk] (None entries when not derivable). Sliced on play_from_here.
        self._view_clock_history: list[tuple[float | None, float | None]] = []
        # Live (white, black) clocks AFTER the imported PGN's final ply. Used
        # by play_from_here when cursor lands at the last ply (no pre-move
        # snapshot beyond the last entry to derive post-move clocks from).
        self._view_final_white: float | None = None
        self._view_final_black: float | None = None
        # Player names from the imported PGN's [White]/[Black] headers,
        # surfaced in clock-row labels while viewing.
        self._view_white_name: str | None = None
        self._view_black_name: str | None = None
        # Per-ply post-move eval (white POV) parsed from PGN comments.
        # None when the PGN had no recognizable eval annotations.
        self._view_eval_history: list[dict | None] | None = None
        self._lock = asyncio.Lock()

    @property
    def engine_path(self) -> str:
        return self._engine_path

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_analyzing(self) -> bool:
        return self._analysis_mode

    def set_engine_options(self, options: dict | None) -> None:
        """Set the UCI options to apply on the next engine launch.

        Updates take effect when the engine is (re)spawned. The API layer
        calls this on every fetch from the registry so registry edits
        propagate to the next game.
        """
        self._engine_options = dict(options or {})

    def set_engine_args(self, args: list[str] | None) -> None:
        """Set the extra argv passed on the next engine launch."""
        self._engine_args = list(args or [])

    def set_engine_env(self, env: dict[str, str] | None) -> None:
        """Set the per-engine env overlay applied on the next launch."""
        self._engine_env = dict(env or {})

    def set_engine_name(self, name: str | None) -> None:
        """Override the display name shown to the user.

        Called by the API layer with the engine-registry name so the clock
        label matches the Engines list. A None/empty value is ignored — it
        does not clear a previously-resolved name (otherwise a fallback
        fetch after the registry entry is removed would wipe the label).
        """
        if name:
            self._engine_name = name

    async def _ensure_engine(self) -> chess.engine.UciProtocol:
        if self._engine is None:
            self._engine = await self._spawn_engine()
            if not self._engine_name:
                self._engine_name = (
                    self._engine.id.get("name") or Path(self._engine_path).name
                )
        return self._engine

    async def _spawn_engine(
        self, overrides: dict | None = None,
    ) -> chess.engine.UciProtocol:
        """Launch a fresh engine process and apply per-engine + global options.

        `overrides` win over both per-engine options and global defaults —
        used by analysis mode to bump Threads on its own throwaway instance.
        Skips unknown/managed options instead of failing (engine schema may
        have drifted since save).
        """
        command: str | list[str] = (
            [self._engine_path, *self._engine_args]
            if self._engine_args else self._engine_path
        )
        popen_kwargs: dict = {}
        if self._engine_env:
            popen_kwargs["env"] = {**os.environ, **self._engine_env}
        _transport, engine = await chess.engine.popen_uci(command, **popen_kwargs)
        rc_future = getattr(engine, "returncode", None)
        if rc_future is not None:
            rc_future.add_done_callback(lambda f: f.exception())
        accepted: dict = {}
        for k, v in (self._engine_options or {}).items():
            if k in engine.options and not engine.options[k].is_managed():
                accepted[k] = v
            else:
                log.warning(
                    "engine %s: skipping unknown/managed option %s",
                    self._engine_path, k,
                )
        for k, v in self._global_engine_defaults().items():
            if k in engine.options and not engine.options[k].is_managed():
                accepted[k] = v
        for k, v in (overrides or {}).items():
            if k in engine.options and not engine.options[k].is_managed():
                accepted[k] = v
        if accepted:
            try:
                await engine.configure(accepted)
            except chess.engine.EngineError:
                log.exception("engine refused options %s", accepted)
        return engine

    def _eval_pov(self, stm: chess.Color = chess.WHITE) -> chess.Color:
        """Resolve play_eval_pov setting → chess.Color for serialization."""
        mode = getattr(self._settings, "play_eval_pov", "white") if self._settings else "white"
        if mode == "human":
            return chess.WHITE if self._human_white else chess.BLACK
        if mode == "engine":
            return stm  # raw UCI: score from the side to move
        return chess.WHITE

    def _global_engine_defaults(self) -> dict:
        """UCI-option subset of the global engine defaults from settings.
        Blank/None entries are dropped so callers can iterate without
        another guard. Book file + plies are fastchess-only and
        excluded — see project_hve_book_followup memory."""
        s = self._settings
        if s is None:
            return {}
        out: dict = {}
        if getattr(s, "engine_default_threads", None):
            out["Threads"] = s.engine_default_threads
        if getattr(s, "engine_default_hash_mb", None):
            out["Hash"] = s.engine_default_hash_mb
        sp = getattr(s, "engine_default_syzygy_path", None)
        if sp:
            out["SyzygyPath"] = sp
        return out

    async def new_game(
        self,
        human_white: bool,
        tc: TimeControl,
        start_fen: str | None = None,
        start_moves_uci: list[str] | None = None,
        seed_clock_history: list[tuple[float | None, float | None]] | None = None,
        seed_final_white_time: float | None = None,
        seed_final_black_time: float | None = None,
    ) -> str:
        """Start a fresh game.

        Optional `start_fen` seeds the board (after replaying any
        `start_moves_uci`). When `seed_clock_history` is supplied (e.g. from
        a PGN with [%clk] comments), per-ply clocks are restored and
        take-back can undo into the seeded plies. None entries fall back
        to `tc.initial_seconds`.
        """
        async with self._lock:
            await self._cancel_analysis()
            self._analysis_mode = False
            await self._cancel_think()
            await self._cancel_tick()
            self._viewing = False
            self._view_full_moves = []
            self._view_clock_history = []
            self._view_final_white = None
            self._view_final_black = None
            self._view_white_name = None
            self._view_black_name = None
            self._view_eval_history = None
            self._view_cursor = 0
            engine = await self._ensure_engine()
            engine.send_line("ucinewgame")
            try:
                board = chess.Board(start_fen) if start_fen else chess.Board()
            except ValueError as e:
                raise RuntimeError(f"invalid FEN: {e}") from e
            for uci in start_moves_uci or []:
                try:
                    move = chess.Move.from_uci(uci)
                except ValueError as e:
                    raise RuntimeError(f"invalid UCI in seed moves: {uci}") from e
                if move not in board.legal_moves:
                    raise RuntimeError(f"illegal seed move: {uci}")
                board.push(move)
            if board.is_game_over():
                raise RuntimeError("seeded position is already over")
            self._board = board
            self._start_fen = start_fen  # None for startpos games
            self._human_white = human_white
            self._tc = tc
            self._turn_started_at = time.monotonic()
            # Seed _clock_history with one snapshot per ply (invariant for
            # takeback). Use the parsed PGN values when available; otherwise
            # synthesize (initial, initial) — clocks are unknown for the
            # seeded plies but the invariant still holds.
            n_plies = len(board.move_stack)
            if seed_clock_history is not None and len(seed_clock_history) == n_plies:
                self._clock_history = [
                    (
                        w if w is not None else tc.initial_seconds,
                        b if b is not None else tc.initial_seconds,
                    )
                    for (w, b) in seed_clock_history
                ]
            else:
                self._clock_history = [
                    (tc.initial_seconds, tc.initial_seconds) for _ in range(n_plies)
                ]
            # Live clocks: use PGN-derived final values when available so the
            # next move continues from the imported state.
            self._white_time = (
                seed_final_white_time
                if seed_final_white_time is not None
                else tc.initial_seconds
            )
            self._black_time = (
                seed_final_black_time
                if seed_final_black_time is not None
                else tc.initial_seconds
            )
            self._paused = False
            self._game_id = uuid.uuid4().hex[:12]
            self._game_started_wall = time.time()
            await self._persist()
            await self._publish_board()
            await self._publish_clock()
        self._start_tick()
        engine_color = chess.BLACK if human_white else chess.WHITE
        if self._board.turn == engine_color:
            await self._engine_to_move()
        return self._game_id

    async def submit_move(self, uci: str) -> None:
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
            if self._viewing:
                raise RuntimeError("view mode is on")
            if self._paused:
                raise RuntimeError("game is paused")
            if self._analysis_mode:
                raise RuntimeError("analysis mode is on")
            if self._board.turn != (chess.WHITE if self._human_white else chess.BLACK):
                raise RuntimeError("not human's turn")
            try:
                move = chess.Move.from_uci(uci)
            except ValueError as e:
                raise RuntimeError(f"invalid uci: {uci}") from e
            if move not in self._board.legal_moves:
                raise RuntimeError(f"illegal move: {uci}")
            # Snapshot clocks BEFORE consuming, so take-back restores the state
            # at the start of this turn.
            self._clock_history.append((self._white_time, self._black_time))
            self._consume_turn_time()
            self._board.push(move)
            await self._persist()
            await self._publish_board()
            await self._publish_clock()
            ended = self._board.is_game_over()
            if ended:
                end_game_id, end_payload = self._finalize_game_locked()
            else:
                self._maybe_save_pgn(result="*", termination="unterminated")
        if ended:
            await self._cancel_tick()
            await self._bus.publish(
                Event(kind="game_result", game_id=end_game_id, payload=end_payload)
            )
        else:
            await self._engine_to_move()

    async def republish_state(self) -> None:
        """Re-emit the current board + clock so a stale client can resync.

        Also kicks off the tick loop (and the engine, if it's its turn) on
        the first call after a restored game (see `restore_from`): the
        side-to-move's clock starts ticking now, not at server-boot time.
        """
        kick_engine = False
        async with self._lock:
            if self._board is None or self._game_id is None:
                return
            # In view mode there is no live game: clocks are frozen at 0,
            # the engine isn't thinking, and starting the tick here would
            # immediately fire a flag-fall on white_time=0.
            if self._viewing:
                await self._publish_board()
                await self._publish_clock()
                return
            if (
                self._turn_started_at is None
                and not self._paused
                and not self._board.is_game_over()
            ):
                self._turn_started_at = time.monotonic()
                self._start_tick()
                engine_color = chess.BLACK if self._human_white else chess.WHITE
                if self._board.turn == engine_color and self._think_task is None:
                    kick_engine = True
            await self._publish_board()
            await self._publish_clock()
        if kick_engine:
            await self._engine_to_move()

    def snapshot_events(self) -> list[Event]:
        if self._board is None or self._game_id is None:
            return []
        return [self._board_event(), self._clock_event()]

    async def takeback(self) -> None:
        """Undo back to the human's turn. Cancels any in-flight engine search.

        - If it's currently the human's turn and the engine just played, pop
          two plies (engine + human's previous).
        - If it's currently the engine's turn (engine is thinking), cancel and
          pop one ply (the human's just-played move).
        - Otherwise no-op.
        """
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
            if self._viewing:
                raise RuntimeError("view mode is on")
            if self._analysis_mode:
                raise RuntimeError("analysis mode is on")
            await self._cancel_think()
            human_color = chess.WHITE if self._human_white else chess.BLACK
            if self._board.turn == human_color:
                # Pop engine's reply, then human's last.
                if len(self._board.move_stack) < 2:
                    raise RuntimeError("nothing to take back")
                self._board.pop()
                self._clock_history.pop()
                self._board.pop()
                wt, bt = self._clock_history.pop()
            else:
                # Engine was thinking; pop the human's last move.
                if len(self._board.move_stack) < 1:
                    raise RuntimeError("nothing to take back")
                self._board.pop()
                wt, bt = self._clock_history.pop()
            self._white_time = wt
            self._black_time = bt
            self._turn_started_at = time.monotonic()
            self._paused = False
            await self._persist()
            await self._publish_board()
            await self._publish_clock()

    async def switch_sides(self) -> None:
        """Swap which color the human plays; engine takes the other side.

        Cancels any in-flight engine search, snaps the side-to-move's
        clock so the human isn't charged for the engine's upcoming think
        (and vice versa), then kicks the engine if it's now its turn.

        Rejected when paused or when the game is over. At any ply,
        including ply 0 (start position).
        """
        kick_engine = False
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
            if self._viewing:
                raise RuntimeError("view mode is on")
            if self._board.is_game_over():
                raise RuntimeError("game is over")
            if self._analysis_mode:
                raise RuntimeError("cannot switch sides during analysis")
            await self._cancel_think()
            # Bake elapsed think time into the side-to-move's clock without
            # crediting the increment (no move was completed). Then restart
            # the timer so the new thinker's clock starts fresh from now.
            # While paused, the clock isn't running so neither step applies.
            if self._turn_started_at is not None:
                elapsed = time.monotonic() - self._turn_started_at
                if self._board.turn == chess.WHITE:
                    self._white_time = max(0.0, self._white_time - elapsed)
                else:
                    self._black_time = max(0.0, self._black_time - elapsed)
                self._turn_started_at = time.monotonic()
            self._human_white = not self._human_white
            await self._persist()
            await self._publish_board()
            await self._publish_clock()
            # Don't kick the engine while paused — resume() handles that.
            if not self._paused:
                engine_color = chess.BLACK if self._human_white else chess.WHITE
                if self._board.turn == engine_color:
                    kick_engine = True
        if kick_engine:
            await self._engine_to_move()

    async def resign(self) -> None:
        async with self._lock:
            if self._viewing:
                raise RuntimeError("view mode is on")
            await self._cancel_analysis()
            self._analysis_mode = False
            await self._cancel_think()
            await self._cancel_tick()
            if self._game_id is None:
                return
            # Result from human's perspective: human resigned -> engine wins.
            result = "0-1" if self._human_white else "1-0"
            self._maybe_save_pgn(result=result, termination="resignation")
            await self._bus.publish(
                Event(
                    kind="game_result",
                    game_id=self._game_id,
                    payload={"result": "resign", "by": "human"},
                )
            )
            self._game_id = None
            self._board = None
            self._clear_store()

    async def pause(self) -> None:
        """Pause the clock. Only valid on the human's turn.

        Bakes elapsed think time into the side-to-move's stored clock
        (without crediting the increment — that fires only on a completed
        move), stops the tick loop, and rejects subsequent submit_move
        calls until resume().
        """
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
            if self._viewing:
                raise RuntimeError("view mode is on")
            if self._board.is_game_over():
                raise RuntimeError("game is over")
            if self._analysis_mode:
                raise RuntimeError("cannot pause during analysis")
            if self._board.turn != (chess.WHITE if self._human_white else chess.BLACK):
                raise RuntimeError("can only pause on your turn")
            if self._paused:
                return
            if self._turn_started_at is not None:
                elapsed = time.monotonic() - self._turn_started_at
                if self._board.turn == chess.WHITE:
                    self._white_time = max(0.0, self._white_time - elapsed)
                else:
                    self._black_time = max(0.0, self._black_time - elapsed)
            self._turn_started_at = None
            self._paused = True
            await self._persist()
            await self._cancel_tick()
            await self._publish_clock()

    async def resume(self) -> None:
        kick_engine = False
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
            if self._viewing:
                raise RuntimeError("view mode is on")
            if not self._paused:
                return
            self._paused = False
            self._turn_started_at = time.monotonic()
            await self._persist()
            # Start the tick under the lock so a racing pause() cannot land
            # between unlock and _start_tick (which would leave the loop
            # running with _paused=True flapping).
            self._start_tick()
            await self._publish_clock()
            if not self._board.is_game_over():
                engine_color = chess.BLACK if self._human_white else chess.WHITE
                if (
                    self._board.turn == engine_color
                    and self._think_task is None
                ):
                    kick_engine = True
        if kick_engine:
            await self._engine_to_move()

    async def start_analysis(self) -> None:
        """Enter UCI go-infinite mode on the current position.

        Only valid from a paused game — that guarantees no engine search
        is in flight and the clock is already frozen.
        """
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
            if self._board.is_game_over():
                raise RuntimeError("game is over")
            if self._analysis_mode:
                return
            # In play mode the game must be paused first (so no engine
            # search is running and clocks are frozen). View mode already
            # satisfies both conditions implicitly.
            if not self._viewing and not self._paused:
                raise RuntimeError("pause the game before entering analysis")
            self._analysis_mode = True
            # Snapshot game_id + board under the lock so the task doesn't
            # need to re-acquire it for setup (avoids a deadlock window
            # against operations that hold the lock while cancelling us).
            game_id = self._game_id
            board = self._board.copy()
            await self._publish_board()
            await self._publish_clock()
        self._analysis_task = asyncio.create_task(self._run_analysis(game_id, board))

    async def stop_analysis(self) -> None:
        """Leave analysis mode. Game stays paused until the user resumes;
        on resume, the engine kicks off if it's its turn."""
        async with self._lock:
            if not self._analysis_mode:
                return
            self._analysis_mode = False
        await self._cancel_analysis()
        async with self._lock:
            if self._board is None or self._game_id is None:
                return
            # Paused-flag is play-mode only; in view mode there's no clock
            # to freeze and play_from_here is the canonical exit.
            if not self._viewing:
                self._paused = True
            self._turn_started_at = None
            await self._persist()
            await self._publish_board()
            await self._publish_clock()

    # ----- view mode -----

    async def enter_view_mode(
        self,
        *,
        start_fen: str | None,
        moves_uci: list[str],
        clock_history: list[tuple[float | None, float | None]] | None,
        final_white_time: float | None = None,
        final_black_time: float | None = None,
        white_name: str | None = None,
        black_name: str | None = None,
        eval_history: list[dict | None] | None = None,
    ) -> str:
        """Load a PGN-imported game into view mode at the LAST ply.

        Replaces any active live game (the autosave file preserves it for
        future load-from-history). No clocks tick, no engine thinks, no
        autosave fires. Navigation is via view_first/back/forward/last.
        Exit via play_from_here, which seeds a fresh play game.
        """
        async with self._lock:
            await self._cancel_analysis()
            self._analysis_mode = False
            await self._cancel_think()
            await self._cancel_tick()
            try:
                start_board = chess.Board(start_fen) if start_fen else chess.Board()
            except ValueError as e:
                raise RuntimeError(f"invalid FEN: {e}") from e
            full_moves: list[chess.Move] = []
            replay = start_board.copy()
            for uci in moves_uci:
                try:
                    move = chess.Move.from_uci(uci)
                except ValueError as e:
                    raise RuntimeError(f"invalid UCI in view moves: {uci}") from e
                if move not in replay.legal_moves:
                    raise RuntimeError(f"illegal move in view: {uci}")
                full_moves.append(move)
                replay.push(move)
            self._viewing = True
            self._view_full_moves = full_moves
            self._view_clock_history = list(clock_history) if clock_history else []
            self._view_final_white = final_white_time
            self._view_final_black = final_black_time
            self._view_white_name = white_name
            self._view_black_name = black_name
            self._view_eval_history = (
                list(eval_history) if eval_history else None
            )
            self._view_cursor = len(full_moves)  # land at last ply
            self._start_fen = start_fen
            self._board = replay  # already at the final position
            self._game_id = uuid.uuid4().hex[:12]
            self._game_started_wall = None  # not a play game; no autosave
            # Clocks frozen — irrelevant in view mode but keep types sane.
            self._white_time = 0.0
            self._black_time = 0.0
            self._turn_started_at = None
            self._clock_history = []
            self._paused = False
            await self._publish_board()
            await self._publish_clock()
        return self._game_id

    async def view_goto(self, ply: int) -> None:
        """Move the view cursor to ``ply`` (0..len(full_moves)). Rebuilds
        the board by replaying from start. Rejected during analysis."""
        async with self._lock:
            if not self._viewing:
                raise RuntimeError("not in view mode")
            if self._analysis_mode:
                raise RuntimeError("analysis is on; stop it before navigating")
            n = len(self._view_full_moves)
            if ply < 0 or ply > n:
                raise RuntimeError(f"ply out of range: {ply} (0..{n})")
            self._view_cursor = ply
            board = (
                chess.Board(self._start_fen) if self._start_fen else chess.Board()
            )
            for m in self._view_full_moves[:ply]:
                board.push(m)
            self._board = board
            await self._publish_board()
            # Clock display reflects historical clocks at the cursor.
            await self._publish_clock()

    async def view_first(self) -> None:
        await self.view_goto(0)

    async def view_back(self) -> None:
        async with self._lock:
            target = max(0, self._view_cursor - 1)
        await self.view_goto(target)

    async def view_forward(self) -> None:
        async with self._lock:
            target = min(len(self._view_full_moves), self._view_cursor + 1)
        await self.view_goto(target)

    async def view_last(self) -> None:
        await self.view_goto(len(self._view_full_moves))

    async def play_from_here(self, tc: TimeControl, inherit_clocks: bool = False) -> str:
        """Exit view mode by starting a fresh play game seeded with plies
        0..cursor. New game_id, new autosave file. Side-to-play is whoever
        is to move at the cursor (matches today's import default).

        ``inherit_clocks``: when True, seed live clocks from the PGN cursor
        (study time pressure / repro engine behavior). When False, live
        clocks reset to ``tc.initial_seconds``.
        """
        async with self._lock:
            if not self._viewing:
                raise RuntimeError("not in view mode")
            if self._analysis_mode:
                raise RuntimeError("analysis is on; stop it before play_from_here")
            cursor = self._view_cursor
            seed_moves = [m.uci() for m in self._view_full_moves[:cursor]]
            seed_clocks = (
                list(self._view_clock_history[:cursor])
                if self._view_clock_history
                else None
            )
            # Live clocks AFTER the seeded plies (post-move-cursor):
            # - cursor at last ply -- use the imported final_*_time (no
            #   pre-move snapshot beyond it exists);
            # - cursor mid-game    -- use the next ply's pre-move snapshot
            #   (pre-move-(cursor+1) == post-move-cursor).
            # Only consulted when inherit_clocks is True.
            seed_final_w: float | None = None
            seed_final_b: float | None = None
            if inherit_clocks and self._view_clock_history:
                if cursor == len(self._view_full_moves):
                    seed_final_w = self._view_final_white
                    seed_final_b = self._view_final_black
                elif cursor < len(self._view_clock_history):
                    nw, nb = self._view_clock_history[cursor]
                    seed_final_w, seed_final_b = nw, nb
            start_fen = self._start_fen
            # Determine side-to-move at the cursor without leaving the lock.
            board = chess.Board(start_fen) if start_fen else chess.Board()
            for m in self._view_full_moves[:cursor]:
                board.push(m)
            # Refuse if the cursor lands on a finished position — would
            # otherwise raise inside new_game AFTER viewer state is cleared,
            # stranding the user in neither view nor play. Caller should
            # nav back first (UI disables the button at game-over plies).
            if board.is_game_over():
                raise RuntimeError("game is over at this ply; back up first")
            human_white = (board.turn == chess.WHITE)
            # Exit view mode before the new_game call (which re-acquires
            # the lock). Clear viewer state so new_game starts clean.
            self._viewing = False
            self._view_full_moves = []
            self._view_clock_history = []
            self._view_final_white = None
            self._view_final_black = None
            self._view_white_name = None
            self._view_black_name = None
            self._view_eval_history = None
            self._view_cursor = 0
        return await self.new_game(
            human_white=human_white,
            tc=tc,
            start_fen=start_fen,
            start_moves_uci=seed_moves,
            seed_clock_history=seed_clocks,
            seed_final_white_time=seed_final_w,
            seed_final_black_time=seed_final_b,
        )

    async def swap_engine(self, path: str) -> None:
        """Replace the engine binary; preserves the active game.

        Per-engine ``args``/``env`` are picked up via the existing
        ``set_engine_args``/``set_engine_env`` setters that the API layer
        already calls on every fetch — they apply to the next spawn after
        the swap, so we don't need to thread them through here.
        """
        kick_engine = False
        async with self._lock:
            await self._cancel_analysis()
            self._analysis_mode = False
            await self._cancel_think()
            self._engine_path = path
            self._engine_name = None
            if self._board is not None and self._game_id is not None:
                await self._publish_board()
                if not self._board.is_game_over() and not self._paused:
                    engine_color = chess.BLACK if self._human_white else chess.WHITE
                    if self._board.turn == engine_color:
                        kick_engine = True
        if kick_engine:
            await self._engine_to_move()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._cancel_analysis()
            self._analysis_mode = False
            await self._cancel_think()
            await self._cancel_tick()
            if self._engine is not None:
                try:
                    await self._engine.quit()
                except (chess.engine.EngineTerminatedError, RuntimeError, BrokenPipeError):
                    pass
                self._engine = None

    # ----- persistence -----

    async def _persist(self) -> None:
        """Snapshot the active game to disk. Call under self._lock.

        The state object is built synchronously (so it captures the values
        at lock-held time), but the disk write happens off the event loop
        via asyncio.to_thread — keeping the WS broadcast loop responsive
        on slow filesystems (network FS, encrypted volumes).
        """
        if self._store is None or self._board is None or self._game_id is None:
            return
        if self._viewing:
            return  # view sessions aren't persisted; the source PGN is on disk
        state = GameState(
            game_id=self._game_id,
            human_white=self._human_white,
            tc_initial_seconds=self._tc.initial_seconds,
            tc_increment_seconds=self._tc.increment_seconds,
            white_time=self._white_time,
            black_time=self._black_time,
            paused=self._paused,
            moves_uci=[m.uci() for m in self._board.move_stack],
            clock_history=[[w, b] for (w, b) in self._clock_history],
            start_fen=self._start_fen,
            game_started_wall=self._game_started_wall,
        )
        try:
            await asyncio.to_thread(self._store.save, state)
        except Exception:
            # Persistence is best-effort: never let a save failure (disk full,
            # serialization quirk, permissions) abort the move that triggered it.
            log.exception("could not persist game state")

    def _clear_store(self) -> None:
        if self._store is not None:
            self._store.clear()

    def restore_from(self, state: GameState) -> None:
        """Rehydrate from a saved snapshot. Engine process is NOT spawned;
        it'll spawn lazily on the first call that needs it (`_ensure_engine`).

        The tick loop is intentionally NOT started here: a server that boots
        into a near-zero clock with no client connected would otherwise time-flag
        before anyone could see the position. The first `republish_state()`
        call (i.e. a client subscribed) starts ticking and resets
        `turn_started_at` so the side-to-move's clock isn't charged for the
        gap between boot and connect.
        """
        self._board = chess.Board(state.start_fen) if state.start_fen else chess.Board()
        self._start_fen = state.start_fen
        for uci in state.moves_uci:
            self._board.push(chess.Move.from_uci(uci))
        self._game_id = state.game_id
        # Fall back to now() for older saves missing this field — preserves
        # autosave behavior, just renames the file going forward.
        self._game_started_wall = state.game_started_wall or time.time()
        self._human_white = state.human_white
        self._tc = TimeControl(
            initial_seconds=state.tc_initial_seconds,
            increment_seconds=state.tc_increment_seconds,
        )
        self._white_time = state.white_time
        self._black_time = state.black_time
        self._paused = state.paused
        self._clock_history = [(w, b) for (w, b) in state.clock_history]
        # Marker: turn hasn't started ticking yet. republish_state() sets it.
        self._turn_started_at = None

    # ----- internals -----

    def _consume_turn_time(self) -> None:
        """Subtract elapsed wall time from the side that just moved; add increment."""
        if self._turn_started_at is None or self._board is None:
            return
        elapsed = time.monotonic() - self._turn_started_at
        # `board.turn` here is the side that JUST moved (we haven't pushed yet
        # when called from submit_move; for engine moves, _think_and_play
        # calls this just before push too).
        side_just_moved = self._board.turn
        if side_just_moved == chess.WHITE:
            self._white_time = max(0.0, self._white_time - elapsed) + self._tc.increment_seconds
        else:
            self._black_time = max(0.0, self._black_time - elapsed) + self._tc.increment_seconds
        self._turn_started_at = time.monotonic()

    def _remaining(self, side: chess.Color) -> float:
        """Live remaining time for `side`, accounting for ticking-down on the
        side currently thinking."""
        base = self._white_time if side == chess.WHITE else self._black_time
        if (
            self._board is not None
            and not self._board.is_game_over()
            and not self._paused
            and self._board.turn == side
            and self._turn_started_at is not None
        ):
            base = max(0.0, base - (time.monotonic() - self._turn_started_at))
        return base

    async def _cancel_think(self) -> None:
        self._think_gen += 1
        if self._engine is not None:
            try:
                t = getattr(self._engine, "transport", None)
                if t is not None:
                    t.close()
            except Exception:
                log.exception("error tearing down engine")
            self._engine = None
        if self._think_task and not self._think_task.done():
            self._think_task.cancel()
        self._think_task = None
        self._analysis = None

    async def _cancel_analysis(self) -> None:
        """Stop the infinite-analysis loop gracefully.

        Sends UCI `stop` via AnalysisResult.stop(), then awaits the task
        briefly. Falls back to task cancellation if the engine doesn't
        wind down within the grace period — keeps the engine process
        alive across analysis toggles (avoids re-warming hash/NN).
        """
        if self._analysis is not None:
            try:
                self._analysis.stop()
            except Exception:
                pass
        if self._analysis_task and not self._analysis_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._analysis_task), timeout=2.0)
            except asyncio.TimeoutError:
                self._analysis_task.cancel()
                try:
                    await self._analysis_task
                except (asyncio.CancelledError, Exception):
                    pass
            except (asyncio.CancelledError, Exception):
                pass
        self._analysis_task = None
        self._analysis = None

    async def _cancel_tick(self) -> None:
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
            try:
                await self._tick_task
            except (asyncio.CancelledError, Exception):
                pass
        self._tick_task = None

    def _start_tick(self) -> None:
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        try:
            while self._game_id is not None and self._board is not None:
                if self._board.is_game_over():
                    break
                await self._publish_clock()
                # Detect flag fall.
                if self._remaining(self._board.turn) <= 0.0:
                    await self._handle_flag_fall()
                    return
                await asyncio.sleep(CLOCK_TICK_INTERVAL)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("clock tick loop crashed")

    async def _handle_flag_fall(self) -> None:
        async with self._lock:
            if self._game_id is None or self._board is None:
                return
            loser = "white" if self._board.turn == chess.WHITE else "black"
            game_id = self._game_id
            await self._cancel_think()
            # Loser is the side to move when the flag fell.
            result = "0-1" if loser == "white" else "1-0"
            self._maybe_save_pgn(result=result, termination="time_forfeit")
        await self._bus.publish(
            Event(
                kind="game_result",
                game_id=game_id,
                payload={"result": "timeout", "loser": loser},
            )
        )
        async with self._lock:
            # Only clear if the same game is still active. A racing
            # new_game / enter_view_mode between the two critical sections
            # may have replaced the game; in that case we must not trample
            # the freshly-installed state.
            if self._game_id == game_id:
                self._game_id = None
                self._board = None
                self._clear_store()

    async def _engine_to_move(self) -> None:
        self._think_task = asyncio.create_task(self._think_and_play())

    async def _think_and_play(self) -> None:
        async with self._lock:
            if self._board is None or self._game_id is None:
                return
            game_id = self._game_id
            board = self._board
            gen = self._think_gen
            try:
                engine = await self._ensure_engine()
            except Exception:
                log.exception("could not start engine for search")
                return
        # Use the live remaining time, not the snapshot at turn start.
        white_clock = self._remaining(chess.WHITE)
        black_clock = self._remaining(chess.BLACK)
        limit = chess.engine.Limit(
            white_clock=white_clock,
            black_clock=black_clock,
            white_inc=self._tc.increment_seconds,
            black_inc=self._tc.increment_seconds,
        )
        try:
            with await engine.analysis(board, limit=limit) as analysis:
                self._analysis = analysis
                async for info in analysis:
                    if "pv" in info or "depth" in info or "score" in info:
                        await self._bus.publish(
                            Event(
                                kind="engine_info",
                                game_id=game_id,
                                payload=_serialize_info(info, board, self._eval_pov(board.turn)),
                            )
                        )
                result = analysis.wait()  # returns BestMove
                best_move = await result
                best = best_move.move
                if best is None:
                    return
        except chess.engine.EngineTerminatedError:
            log.exception("engine terminated mid-search")
            await self._bus.publish(
                Event(kind="system", game_id=game_id, payload={"error": "engine_terminated"})
            )
            return
        except (asyncio.CancelledError, RuntimeError, BrokenPipeError):
            return
        finally:
            self._analysis = None
        async with self._lock:
            if (
                self._board is None
                or self._game_id != game_id
                or self._think_gen != gen
            ):
                # Search was cancelled; ignore its bestmove.
                return
            self._clock_history.append((self._white_time, self._black_time))
            self._consume_turn_time()
            self._board.push(best)
            await self._persist()
            await self._publish_board()
            await self._publish_clock()
            ended = self._board.is_game_over()
            if ended:
                end_game_id, end_payload = self._finalize_game_locked()
            else:
                self._maybe_save_pgn(result="*", termination="unterminated")
        if ended:
            await self._cancel_tick()
            await self._bus.publish(
                Event(kind="game_result", game_id=end_game_id, payload=end_payload)
            )

    async def _run_analysis(self, game_id: str, board: chess.Board) -> None:
        """Drive analysis on a dedicated engine instance.

        A throwaway process avoids re-configuring + reverting Threads on the
        play engine (and any state bleed it could cause). Killed on exit.
        """
        overrides: dict = {}
        s = self._settings
        n = getattr(s, "engine_default_analysis_threads", None) if s else None
        if n:
            overrides["Threads"] = n
        try:
            engine = await self._spawn_engine(overrides=overrides)
        except Exception:
            log.exception("could not start engine for analysis")
            return
        try:
            with await engine.analysis(board) as analysis:
                self._analysis = analysis
                async for info in analysis:
                    if "pv" in info or "depth" in info or "score" in info:
                        await self._bus.publish(
                            Event(
                                kind="engine_info",
                                game_id=game_id,
                                payload=_serialize_info(info, board, self._eval_pov(board.turn)),
                            )
                        )
        except chess.engine.EngineTerminatedError:
            log.exception("engine terminated mid-analysis")
            await self._bus.publish(
                Event(kind="system", game_id=game_id, payload={"error": "engine_terminated"})
            )
        except (asyncio.CancelledError, RuntimeError, BrokenPipeError):
            return
        finally:
            self._analysis = None
            try:
                await engine.quit()
            except (chess.engine.EngineTerminatedError, RuntimeError, BrokenPipeError):
                pass

    def _board_event(self) -> Event:
        assert self._board is not None and self._game_id is not None
        opening_payload = None
        # Opening book lookup keys on UCI moves from the standard starting
        # position; an imported (non-startpos) game can't be classified.
        if (
            self._openings is not None
            and self._board.move_stack
            and self._start_fen is None
        ):
            ucis = [m.uci() for m in self._board.move_stack]
            hit = self._openings.lookup(ucis)
            if hit is not None:
                opening_payload = {"eco": hit.eco, "name": hit.name}
        # In view mode, moves_san reflects the FULL game (so the UI can show
        # the whole list with the cursor highlighting one ply); in play mode
        # it's just the moves on the live board.
        if self._viewing:
            full_board = (
                chess.Board(self._start_fen) if self._start_fen else chess.Board()
            )
            for m in self._view_full_moves:
                full_board.push(m)
            moves_san = _moves_san(full_board, self._start_fen)
        else:
            moves_san = _moves_san(self._board, self._start_fen)
        view_payload = None
        if self._viewing:
            # Eval at the cursor = eval recorded for the last played move.
            # cursor==0 means initial position, no move yet -> no eval.
            eval_at_cursor = None
            if (
                self._view_eval_history is not None
                and 0 < self._view_cursor <= len(self._view_eval_history)
            ):
                eval_at_cursor = self._view_eval_history[self._view_cursor - 1]
            view_payload = {
                "cursor": self._view_cursor,
                "total_plies": len(self._view_full_moves),
                "white_name": self._view_white_name,
                "black_name": self._view_black_name,
                "eval": eval_at_cursor,
                # UI disables Play-from-here when the cursor lands on a
                # finished position (mirror of the backend guard).
                "game_over": (outcome := self._board.outcome()) is not None,
                **(
                    {
                        "result": outcome.result(),
                        "termination": outcome.termination.name.lower(),
                    }
                    if outcome is not None
                    else {}
                ),
            }
        return Event(
            kind="board_update",
            game_id=self._game_id,
            payload={
                "fen": self._board.fen(),
                "turn": "white" if self._board.turn else "black",
                "ply": self._board.ply(),
                "moves_san": moves_san,
                "last_move": self._board.peek().uci() if self._board.move_stack else None,
                # human_white is meaningless in view mode (the user isn't
                # playing); omit so the UI's local flip isn't clobbered.
                "human_white": None if self._viewing else self._human_white,
                "engine_name": self._engine_name,
                "opening": opening_payload,
                "tablebase": None,
                "analyzing": self._analysis_mode,
                "view": view_payload,
            },
        )

    def _clock_event(self) -> Event:
        assert self._board is not None and self._game_id is not None
        if self._viewing:
            # Historical clocks at the cursor when the imported PGN carried
            # [%clk]; otherwise null so the UI can render dashes. Either way
            # the clocks are frozen and the UI should style them as disabled.
            wt: float | None = None
            bt: float | None = None
            if self._view_clock_history:
                idx = min(self._view_cursor, len(self._view_clock_history) - 1)
                wt, bt = self._view_clock_history[idx]
            return Event(
                kind="clock_tick",
                game_id=self._game_id,
                payload={
                    "white_time": wt,
                    "black_time": bt,
                    "turn": "white" if self._board.turn else "black",
                    "running": False,
                    "paused": False,
                    "analyzing": self._analysis_mode,
                    "viewing": True,
                },
            )
        return Event(
            kind="clock_tick",
            game_id=self._game_id,
            payload={
                "white_time": self._remaining(chess.WHITE),
                "black_time": self._remaining(chess.BLACK),
                "turn": "white" if self._board.turn else "black",
                "running": not self._board.is_game_over()
                and not self._paused
                and not self._analysis_mode,
                "paused": self._paused,
                "analyzing": self._analysis_mode,
            },
        )

    async def _publish_board(self) -> None:
        await self._bus.publish(self._board_event())

    async def _publish_clock(self) -> None:
        if self._game_id is None or self._board is None:
            return
        await self._bus.publish(self._clock_event())

    def _finalize_game_locked(self) -> tuple[str, dict]:
        """Caller MUST hold self._lock — clearing in the same critical
        section as the game-over detection prevents /game/sync from
        republishing the just-ended board as if the game were live."""
        assert self._lock.locked(), "_finalize_game_locked called without lock"
        assert self._board is not None and self._game_id is not None
        outcome = self._board.outcome()
        result = outcome.result() if outcome else "*"
        termination = outcome.termination.name.lower() if outcome else "unknown"
        self._maybe_save_pgn(result=result, termination=termination)
        self._clear_store()
        game_id = self._game_id
        self._game_id = None
        self._board = None
        return game_id, {"result": result, "termination": termination}

    def _maybe_save_pgn(self, *, result: str, termination: str) -> Path | None:
        if self._board is None or self._game_id is None:
            return None
        if self._viewing:
            return None  # not a play game; autosave is play-mode only
        if self._settings is None or not getattr(self._settings, "pgn_autosave", False):
            return None
        if not self._board.move_stack:
            return None  # nothing worth saving

        raw = getattr(self._settings, "pgn_dir", None)
        if not raw:
            return None
        pgn_dir = Path(raw).expanduser()
        try:
            pgn_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("could not create PGN dir %s", pgn_dir)
            return None

        game = chess.pgn.Game.from_board(self._board)
        engine_label = self._engine_name or Path(self._engine_path).name
        white = "Human" if self._human_white else engine_label
        black = engine_label if self._human_white else "Human"
        game.headers["Event"] = "Sturddle View — Human vs Engine"
        game.headers["Site"] = "Sturddle View"
        game.headers["Date"] = datetime.date.today().strftime("%Y.%m.%d")
        game.headers["White"] = white
        game.headers["Black"] = black
        game.headers["Result"] = result
        game.headers["Termination"] = termination
        if self._tc.initial_seconds:
            game.headers["TimeControl"] = (
                f"{int(self._tc.initial_seconds)}+{int(self._tc.increment_seconds)}"
            )

        # Opening header: longest registered prefix wins (sticky across
        # transpositions and out-of-book moves). Skipped for FEN-imported
        # games — lookup keys on move history from startpos.
        if self._openings is not None and self._start_fen is None:
            ucis = [m.uci() for m in self._board.move_stack]
            hit = self._openings.lookup(ucis)
            if hit is not None:
                game.headers["ECO"] = hit.eco
                game.headers["Opening"] = hit.name

        # Per-ply [%clk] annotations. _clock_history[i] is (white, black)
        # BEFORE ply i; the mover's clock AFTER ply i is _clock_history[i+1]
        # for that side, or the live clock if i is the most recent ply.
        # Mover is taken from the replay board (handles non-startpos games
        # where ply 0 may be Black to move).
        replay = chess.Board(self._start_fen) if self._start_fen else chess.Board()
        nodes = list(game.mainline())
        for i, node in enumerate(nodes):
            mover_white = (replay.turn == chess.WHITE)
            replay.push(self._board.move_stack[i])
            if i + 1 < len(self._clock_history):
                w_after, b_after = self._clock_history[i + 1]
            else:
                w_after, b_after = self._white_time, self._black_time
            node.set_clock(w_after if mover_white else b_after)

        # Game-start timestamp keeps the path stable across per-move autosaves
        # and the final end-of-game write, so the file is overwritten in place.
        wall = self._game_started_wall or time.time()
        ts = datetime.datetime.fromtimestamp(wall).strftime("%Y%m%d-%H%M%S")
        path = pgn_dir / f"{ts}-{self._game_id}.pgn"
        try:
            atomic_write_text(path, f"{game}\n\n")
        except OSError:
            log.exception("could not write PGN to %s", path)
            return None
        log.info("saved PGN to %s", path)
        return path
