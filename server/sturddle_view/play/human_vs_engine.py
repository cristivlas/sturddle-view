"""Human vs engine driver, on top of python-chess's async UCI interface.

Skips the tournament manager and proxy entirely. Streams engine info (depth,
score, PV, NPS), clock ticks, and game state onto the same event bus the
tournament path uses.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import chess
import chess.engine
import chess.pgn

from .._atomic import atomic_write_text

if TYPE_CHECKING:
    from ..recent_imports import RecentImports
from ..chess.board import board_from, moves_san as _moves_san, side_to_move
from ..chess.pgn_build import build_pgn
from .canonical_hash import canonical_hash
from ..chess.results import DRAW, loser_result, winner_result
from ..events import Event, EventBus
from .chess_clock import ChessClock, TimeControl
from .engine_analysis import (
    EVAL_POV_HUMAN,
    EVAL_POV_WHITE,
    global_engine_defaults,
    resolve_eval_pov_white_or_stm,
    spawn_analysis_engine,
)
from .engine_info_pump import pump_engine_info
from .engine_supervisor import EngineSupervisor
from .game_store import DEFAULT_PLAYER_NAME, GameState, GameStore
from .import_position import explain_invalid
from .mode import Mode, ModeConflictError, Op
from .tablebase import TablebaseProber

log = logging.getLogger(__name__)

CLOCK_TICK_INTERVAL = 0.25  # seconds


@dataclass
class _ViewSnapshot:
    """All view-mode state captured at enter_edit_mode for lossless restore.
    Adding a view-mode field? Add it here too -- single source of truth."""
    start_fen: str | None
    board: chess.Board
    cursor: int
    full_moves: list[chess.Move]
    clock_history: list[tuple[float, float]]
    final_white: float | None
    final_black: float | None
    white_name: str | None
    black_name: str | None
    eval_history: list[dict | None] | None
    comments: list[str | None] | None
    root_comment: str | None
    pgn_result: str | None
    pgn_termination: str | None
    view_hash: str | None = None
    view_summary: dict | None = None
    view_original_text: str | None = None
    view_edited: bool = False


@dataclass
class ViewModeParams:
    start_fen: str | None
    moves_uci: list[str]
    clock_history: list[tuple[float | None, float | None]] | None
    final_white_time: float | None = None
    final_black_time: float | None = None
    white_name: str | None = None
    black_name: str | None = None
    eval_history: list[dict | None] | None = None
    comments: list[str | None] | None = None
    root_comment: str | None = None
    pgn_result: str | None = None
    pgn_termination: str | None = None
    view_hash: str | None = None
    view_summary: dict | None = None
    # Verbatim import text (PGN or FEN). When set, get_pgn_text() can
    # return this directly to avoid any re-serialization loss. Treated as
    # write-once on the HVE side -- see _view_original_text.
    view_original_text: str | None = None


class HumanVsEngine:
    """Single-game driver. Holds one active game at a time."""

    def __init__(
        self,
        engine_path: str,
        bus: EventBus,
        openings=None,
        settings=None,
        store: GameStore | None = None,
        recents: "RecentImports | None" = None,
    ) -> None:
        self._bus = bus
        self._openings = openings  # Optional[OpeningBook]
        self._settings = settings  # Optional[Settings]
        self._store = store
        # Optional RecentImports. When set, finished games are saved into
        # the imports store on game-end so they survive reloads and appear
        # in the recents dropdown. Tagged with summary["source"]="play".
        self._recents = recents
        # UCI engine session: spawn/configure/cancel/quit/swap + log fanout.
        # HVE keeps the search loops (_think_and_play, _run_analysis) and
        # only delegates process lifecycle.
        self._supervisor = EngineSupervisor(
            engine_path=engine_path, bus=bus, settings=settings,
        )
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
        self._player_name: str = DEFAULT_PLAYER_NAME
        self._clock: ChessClock = ChessClock(TimeControl(300.0, 0.0))
        self._think_task: asyncio.Task | None = None
        self._analysis = None  # active chess.engine.AnalysisResult, if any
        self._think_gen: int = 0  # search generation; bumped on cancel
        self._tick_task: asyncio.Task | None = None
        self._mode: Mode = Mode.PLAY
        # Mode before entering ANALYZING; restored by stop_analysis().
        self._pre_analysis_mode: Mode = Mode.PLAY
        self._analysis_task: asyncio.Task | None = None
        # Most recent engine_info payload published during analysis. Kept
        # so /game/sync can re-emit it after a client remount (e.g. user
        # navigated away and back); without this the board arrow stays
        # gone until the engine ships its next info line, which can take
        # seconds at higher depths.
        self._last_analysis_info: dict | None = None
        # View mode: cursor-based playback of an imported / loaded game.
        # _board is rebuilt from _view_full_moves[:_view_cursor] on every
        # navigation, so analyze sees the right position automatically.
        # Autosave, submit_move, engine thinking, and clocks are all gated
        # off while viewing. Exits via play_from_here.
        #
        # Edit mode: entered from view only; commit/cancel return to VIEWING.
        self._edit_pre_fen: str | None = None
        self._edit_view_snapshot: _ViewSnapshot | None = None
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
        # Per-ply sanitized PGN comments (machine annotations stripped).
        # None when the PGN had no commentary at all.
        self._view_comments: list[str | None] | None = None
        # Pre-game / Annotator commentary, sanitized. Shown at cursor==0.
        self._view_root_comment: str | None = None
        # PGN [Result]/[Termination] from the imported game (None when
        # not in view mode). Read by _board_event's view payload.
        self._view_pgn_result: str | None = None
        self._view_pgn_termination: str | None = None
        # SHA-256 hash and human-readable summary of the viewed game's source
        # text (PGN or FEN). None for play-mode games and view/start transitions.
        self._view_hash: str | None = None
        self._view_summary: dict | None = None
        # Original bytes the user pasted, set once in enter_view_mode and never
        # updated. Served verbatim by get_pgn_text when state hasn't diverged;
        # also enables a future revert-to-original.
        self._view_original_text: str | None = None
        # True when view state has diverged from _view_original_text (e.g. after
        # an annotation edit). Forces get_pgn_text to re-serialize.
        self._view_edited: bool = False
        # Per-ply engine eval (white POV), one entry per pushed move. None
        # entries for plies with no engine search (human moves). Matches
        # move_stack length; pop alongside on take-back. Reset on new game.
        self._eval_history: list[dict | None] = []
        # Play-side PGN comments. Populated only when the play game was
        # seeded from a view-mode position (play_from_here) that carried
        # commentary -- otherwise None. Used by _build_play_game_pgn so
        # imported annotations survive the view -> play fork into recents
        # and exports. Live play does not mutate these today.
        self._play_comments: list[str | None] | None = None
        self._play_root_comment: str | None = None
        # Set inside the game-end lock by _stash_recents_payload(); drained
        # after the lock by _flush_recents_save(). Carries (text, summary,
        # game_id) for the recent-imports save so the async write happens
        # outside the critical section.
        self._pending_recents_save: tuple[str, dict, str] | None = None
        # X-game navigation fork link. Set by play_from_here(cursor>=1):
        # (parent_game_id, fork_ply). Drained by paths that durably save
        # the child to recents (finalization via _flush_recents_save and
        # commit_edit via the api layer). Cleared at the top of new_game
        # so plain "new game" and import-on-top never carry a stale link
        # forward; play_from_here re-stashes after new_game returns.
        self._fork_link: tuple[str, int] | None = None
        self._tb: TablebaseProber | None = None
        self._lock = asyncio.Lock()

    @property
    def engine_path(self) -> str:
        return self._supervisor.engine_path

    # Derived bool views -- tests and API layer read these directly.
    # _viewing is True in VIEWING, EDITING, and ANALYZING-from-view:
    # pre-refactor _viewing=True was never cleared when analysis started,
    # so all 6 call sites that read self._viewing must see True whenever
    # the session originated from a view (no live game, frozen clock).
    _VIEW_MASK = int(Mode.VIEWING | Mode.EDITING)

    @property
    def _paused(self) -> bool:
        return self._mode is Mode.PAUSED

    @property
    def _analysis_mode(self) -> bool:
        return self._mode is Mode.ANALYZING

    @property
    def _clock_running(self) -> bool:
        """Clock ticks only in live play: board present, mode is PLAY, game
        not over. Shared by republish_state (gate to start the tick) and
        _clock_event (advertises `running` to clients)."""
        return (
            self._board is not None
            and self._mode is Mode.PLAY
            and not self._board.is_game_over()
        )

    @property
    def _viewing(self) -> bool:
        if self._mode & self._VIEW_MASK:
            return True
        # ANALYZING entered from a view session must still appear as viewing
        # to _clock_event, _board_event, _persist, etc.
        return self._mode is Mode.ANALYZING and self._pre_analysis_mode is Mode.VIEWING

    @property
    def _editing(self) -> bool:
        return self._mode is Mode.EDITING

    @property
    def game_id(self) -> str | None:
        return self._game_id

    @property
    def is_paused(self) -> bool:
        return self._mode is Mode.PAUSED

    @property
    def is_analyzing(self) -> bool:
        return self._mode is Mode.ANALYZING

    @property
    def is_editing(self) -> bool:
        return self._mode is Mode.EDITING

    # ----- accessors for the AI start path. Surface read-only views of
    # internals so api/_ai_kick.py doesn't reach across the abstraction.
    # Returning Optionals (vs raising) keeps the call site branchless.

    def current_board(self) -> chess.Board | None:
        return self._board

    def start_fen(self) -> str | None:
        return self._start_fen

    def view_full_moves_san(self) -> list[str]:
        """Full game's moves when HVE is in view mode (or analyzing-from-
        view); empty in play mode."""
        if self._view_full_moves:
            return self._view_moves_san()
        return []

    def pre_analysis_mode(self) -> "Mode | None":
        return self._pre_analysis_mode

    def engine_display_name(self) -> str | None:
        return self._engine_name

    def viewed_pgn_result(self) -> str | None:
        """PGN result tag of the game currently being viewed
        ('1-0', '0-1', '1/2-1/2', '*'), or None when not in view mode."""
        return self._view_pgn_result

    def lookup_opening(self):
        """Most-specific opening reached. Covers play (live move_stack) and
        view (full PGN); returns None when no book is loaded, no moves yet,
        or no registered line matches."""
        if self._openings is None:
            return None
        if self._view_full_moves:
            ucis = [m.uci() for m in self._view_full_moves]
        elif self._board.move_stack and self._start_fen is None:
            ucis = [m.uci() for m in self._board.move_stack]
        else:
            return None
        return self._openings.lookup(ucis)

    # ----- supervisor passthroughs: tests and API layer still poke these
    # attributes directly on the HVE object; preserve the access pattern
    # so the supervisor extraction is a no-op at the call site.

    @property
    def _engine(self) -> chess.engine.UciProtocol | None:
        return self._supervisor.engine

    @_engine.setter
    def _engine(self, value: chess.engine.UciProtocol | None) -> None:
        self._supervisor.engine = value

    @property
    def _engine_path(self) -> str:
        return self._supervisor.engine_path

    @_engine_path.setter
    def _engine_path(self, value: str) -> None:
        self._supervisor.engine_path = value

    @property
    def _engine_name(self) -> str | None:
        return self._supervisor.engine_name

    @_engine_name.setter
    def _engine_name(self, value: str | None) -> None:
        self._supervisor.engine_name = value

    @property
    def _engine_options(self) -> dict:
        return self._supervisor.options

    @_engine_options.setter
    def _engine_options(self, value: dict) -> None:
        self._supervisor.options = value

    @property
    def _engine_args(self) -> list[str]:
        return self._supervisor.args

    @_engine_args.setter
    def _engine_args(self, value: list[str]) -> None:
        self._supervisor.args = value

    @property
    def _engine_env(self) -> dict[str, str]:
        return self._supervisor.env

    @_engine_env.setter
    def _engine_env(self, value: dict[str, str]) -> None:
        self._supervisor.env = value

    def set_engine_options(self, options: dict | None) -> None:
        """Set the UCI options to apply on the next engine launch.

        Updates take effect when the engine is (re)spawned. The API layer
        calls this on every fetch from the registry so registry edits
        propagate to the next game.
        """
        self._supervisor.options = options or {}

    def set_engine_args(self, args: list[str] | None) -> None:
        """Set the extra argv passed on the next engine launch."""
        self._supervisor.args = args or []

    def set_engine_env(self, env: dict[str, str] | None) -> None:
        """Set the per-engine env overlay applied on the next launch."""
        self._supervisor.env = env or {}

    def set_engine_name(self, name: str | None) -> None:
        """Override the display name shown to the user.

        Called by the API layer with the engine-registry name so the clock
        label matches the Engines list. A None/empty value is ignored -- it
        does not clear a previously-resolved name (otherwise a fallback
        fetch after the registry entry is removed would wipe the label).
        """
        if name:
            self._supervisor.engine_name = name

    async def _ensure_engine(self) -> chess.engine.UciProtocol:
        return await self._supervisor.ensure(
            global_defaults=self._global_engine_defaults(),
        )

    async def _spawn_engine(
        self, overrides: dict | None = None,
    ) -> chess.engine.UciProtocol:
        return await self._supervisor.spawn(
            overrides=overrides,
            global_defaults=self._global_engine_defaults(),
        )

    def _engine_color(self) -> chess.Color:
        return chess.BLACK if self._human_white else chess.WHITE

    def _reset_view_state(self) -> None:
        self._view_full_moves = []
        self._view_clock_history = []
        self._view_final_white = None
        self._view_final_black = None
        self._view_white_name = None
        self._view_black_name = None
        self._view_eval_history = None
        self._view_comments = None
        self._view_root_comment = None
        self._view_pgn_result = None
        self._view_pgn_termination = None
        self._view_hash = None
        self._view_summary = None
        self._view_original_text = None
        self._view_edited = False
        self._view_cursor = 0

    def _eval_pov(self, stm: chess.Color = chess.WHITE) -> chess.Color:
        """Resolve play_eval_pov setting -> chess.Color for serialization.
        Handles 'human' via _human_white; delegates white/engine to
        the shared helper so tools and HVE stay in sync."""
        mode = (
            getattr(self._settings, "play_eval_pov", EVAL_POV_WHITE)
            if self._settings else EVAL_POV_WHITE
        )
        if mode == EVAL_POV_HUMAN:
            return chess.WHITE if self._human_white else chess.BLACK
        return resolve_eval_pov_white_or_stm(self._settings, stm)

    def _global_engine_defaults(self) -> dict:
        """Delegates to the shared engine-analysis helper so HVE,
        the AI analysis tool, and any future caller derive engine
        defaults from one place."""
        return global_engine_defaults(self._settings)

    def _ensure_tablebase(self) -> None:
        sp = getattr(self._settings, "engine_default_syzygy_path", None)
        if self._tb is not None and self._tb.path != sp:
            self._tb.close()
            self._tb = None
        if self._tb is None and sp:
            self._tb = TablebaseProber(sp)

    async def new_game(
        self,
        human_white: bool,
        tc: TimeControl,
        player_name: str = DEFAULT_PLAYER_NAME,
        start_fen: str | None = None,
        start_moves_uci: list[str] | None = None,
        seed_clock_history: list[tuple[float | None, float | None]] | None = None,
        seed_final_white_time: float | None = None,
        seed_final_black_time: float | None = None,
        seed_comments: list[str | None] | None = None,
        seed_root_comment: str | None = None,
    ) -> str:
        """Start a fresh game.

        Optional `start_fen` seeds the board (after replaying any
        `start_moves_uci`). When `seed_clock_history` is supplied (e.g. from
        a PGN with [%clk] comments), per-ply clocks are restored and
        take-back can undo into the seeded plies. None entries fall back
        to `tc.initial_seconds`.

        `seed_comments` / `seed_root_comment` populate the play-side
        comment storage when forking from a view game (play_from_here),
        so imported annotations survive into the eventual PGN export and
        recents save.
        """
        async with self._lock:
            if not (self._mode & Op.NEW_GAME._mask):
                raise ModeConflictError(self._mode, Op.NEW_GAME)
            await self._cancel_analysis()
            await self._cancel_think()
            await self._cancel_tick()
            self._reset_view_state()
            # Universal reset point: any stale fork link from a prior
            # session must not leak into the new game. play_from_here
            # re-stashes after new_game returns; all other paths
            # (plain new game, import-on-top) start link-free.
            self._fork_link = None
            self._ensure_tablebase()
            engine = await self._ensure_engine()
            engine.send_line("ucinewgame")
            try:
                board = board_from(start_fen)
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
            self._player_name = player_name or DEFAULT_PLAYER_NAME
            self._clock = ChessClock(tc)
            self._clock.reseed_from_pgn(
                n_plies=len(board.move_stack),
                seed_history=seed_clock_history,
                final_w=seed_final_white_time,
                final_b=seed_final_black_time,
            )
            self._eval_history = [None] * len(board.move_stack)
            # Seed play-side comments from a forking caller (play_from_here).
            # Truncate/pad the seed to match move_stack length so subsequent
            # take-back can shrink alongside it.
            n_plies = len(board.move_stack)
            if seed_comments is not None:
                seeded = list(seed_comments[:n_plies])
                seeded.extend([None] * (n_plies - len(seeded)))
                self._play_comments = seeded if any(c is not None for c in seeded) else None
            else:
                self._play_comments = None
            self._play_root_comment = seed_root_comment or None
            self._clock.start_turn()
            self._mode = Mode.PLAY
            self._game_id = str(uuid.uuid4())
            self._game_started_wall = time.time()
            await self._persist()
            await self._publish_board()
            await self._publish_clock()
        self._start_tick()
        if self._board.turn == self._engine_color():
            await self._engine_to_move()
        return self._game_id

    async def submit_move(self, uci: str) -> None:
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
            if not (self._mode & Op.SUBMIT_MOVE._mask):
                raise ModeConflictError(self._mode, Op.SUBMIT_MOVE)
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
            self._clock.append_snapshot()
            self._consume_turn_time()
            self._board.push(move)
            # Human ply: no engine search, eval slot is None.
            self._eval_history.append(None)
            if self._play_comments is not None:
                self._play_comments.append(None)
            await self._persist()
            await self._publish_board()
            await self._publish_clock()
            ended = self._is_ended()
            if ended:
                end_game_id, end_payload = self._finalize_game_locked()
            else:
                self._maybe_save_pgn(result="*", termination="unterminated")
        if ended:
            await self._cancel_tick()
            await self._bus.publish(
                Event(kind="game_result", game_id=end_game_id, payload=end_payload)
            )
            await self._flush_recents_save()
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
                await self._republish_last_analysis_info()
                return
            if self._clock.turn_started_at is None and self._clock_running:
                self._clock.start_turn()
                self._start_tick()
                if self._board.turn == self._engine_color() and self._think_task is None:
                    kick_engine = True
            await self._publish_board()
            await self._publish_clock()
            await self._republish_last_analysis_info()
        if kick_engine:
            await self._engine_to_move()

    async def _republish_last_analysis_info(self) -> None:
        """Re-emit the most recent analysis info payload so a freshly
        mounted client can restore the board arrow without waiting for
        the engine's next info line. No-op when not analyzing or when
        no info has been captured yet (engine hasn't produced one)."""
        if not self._analysis_mode or self._last_analysis_info is None:
            return
        await self._bus.publish(
            Event(
                kind="engine_info",
                game_id=self._game_id,
                payload=self._last_analysis_info,
            )
        )

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
            if not (self._mode & Op.TAKEBACK._mask):
                raise ModeConflictError(self._mode, Op.TAKEBACK)
            await self._cancel_think()
            def _pop_one() -> None:
                self._board.pop()
                self._clock.pop_snapshot()
                self._eval_history.pop()
                if self._play_comments is not None:
                    self._play_comments.pop()
                    if not any(c is not None for c in self._play_comments):
                        self._play_comments = None

            human_color = chess.WHITE if self._human_white else chess.BLACK
            if self._board.turn == human_color:
                # Pop engine's reply, then human's last.
                if len(self._board.move_stack) < 2:
                    raise RuntimeError("nothing to take back")
                _pop_one()
                _pop_one()
            else:
                # Engine was thinking; pop the human's last move.
                if len(self._board.move_stack) < 1:
                    raise RuntimeError("nothing to take back")
                _pop_one()
            # Preserve pause state across takeback: undoing should not
            # silently resume the clock.
            if self._paused:
                self._clock.stop_turn()
            else:
                self._clock.start_turn()
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
            if not (self._mode & Op.SWITCH_SIDES._mask):
                raise ModeConflictError(self._mode, Op.SWITCH_SIDES)
            if self._board.is_game_over():
                raise RuntimeError("game is over")
            await self._cancel_think()
            # Bake elapsed think time into the side-to-move's clock without
            # crediting the increment (no move was completed). Then restart
            # the timer so the new thinker's clock starts fresh from now.
            # While paused, the clock isn't running so neither step applies.
            self._clock.snap_for_switch(self._board.turn)
            self._human_white = not self._human_white
            await self._persist()
            await self._publish_board()
            await self._publish_clock()
            # Don't kick the engine outside live play. SWITCH_SIDES is also
            # allowed in ANALYZING/VIEWING (board orientation flip only) where
            # the engine must NOT be kicked -- _clock_running enforces that.
            if self._clock_running and self._board.turn == self._engine_color():
                kick_engine = True
        if kick_engine:
            await self._engine_to_move()

    async def resign(self) -> None:
        async with self._lock:
            if not (self._mode & Op.RESIGN._mask):
                raise ModeConflictError(self._mode, Op.RESIGN)
            await self._cancel_analysis()
            self._mode = Mode.PLAY
            await self._cancel_think()
            await self._cancel_tick()
            if self._game_id is None:
                return
            # Result from human's perspective: human resigned -> engine wins.
            result = loser_result(self._human_white)
            self._maybe_save_pgn(result=result, termination="resignation")
            self._stash_recents_payload(result=result, termination="resignation")
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
        await self._flush_recents_save()

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
            if self._mode is Mode.PAUSED:
                return
            if not (self._mode & Op.PAUSE._mask):
                raise ModeConflictError(self._mode, Op.PAUSE)
            if self._board.is_game_over():
                raise RuntimeError("game is over")
            if self._board.turn != (chess.WHITE if self._human_white else chess.BLACK):
                raise RuntimeError("can only pause on your turn")
            self._clock.pause(self._board.turn)
            self._mode = Mode.PAUSED
            await self._persist()
            await self._cancel_tick()
            await self._publish_clock()

    async def resume(self) -> None:
        kick_engine = False
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
            if self._mode is not Mode.PAUSED:
                return
            self._mode = Mode.PLAY
            self._clock.resume()
            await self._persist()
            # Start the tick under the lock so a racing pause() cannot land
            # between unlock and _start_tick (which would leave the loop
            # running with _paused=True flapping).
            self._start_tick()
            await self._publish_clock()
            if not self._board.is_game_over():
                if (
                    self._board.turn == self._engine_color()
                    and self._think_task is None
                ):
                    kick_engine = True
        if kick_engine:
            await self._engine_to_move()

    async def start_analysis(self) -> None:
        """Enter analysis mode on the current position.

        When `settings.ai_enabled` is on, no engine search is launched
        -- the AI agent drives engine use via tool calls instead. Mode
        and state transitions are identical so the client UI is uniform.

        Only valid from a paused game -- guarantees no in-flight search.
        """
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
            if self._mode is Mode.ANALYZING:
                return
            if not (self._mode & Op.START_ANALYSIS._mask):
                raise ModeConflictError(self._mode, Op.START_ANALYSIS)
            if self._board.is_game_over():
                raise RuntimeError("game is over")
            self._pre_analysis_mode = self._mode
            self._mode = Mode.ANALYZING
            # Snapshot game_id + board under the lock so the task doesn't
            # need to re-acquire it for setup (avoids a deadlock window
            # against operations that hold the lock while cancelling us).
            game_id = self._game_id
            board = self._board.copy()
            await self._publish_board()
            await self._publish_clock()
        if not getattr(self._settings, "ai_enabled", False):
            self._analysis_task = asyncio.create_task(self._run_analysis(game_id, board))

    async def stop_analysis(self) -> None:
        """Leave analysis mode. Game stays paused until the user resumes;
        on resume, the engine kicks off if it's its turn."""
        async with self._lock:
            if self._mode is not Mode.ANALYZING:
                return
        await self._cancel_analysis()
        async with self._lock:
            if self._board is None or self._game_id is None:
                return
            # In play mode, land in PAUSED (no engine search, clock frozen).
            # In view mode, restore VIEWING -- no clock to freeze.
            self._mode = Mode.VIEWING if self._pre_analysis_mode is Mode.VIEWING else Mode.PAUSED
            self._clock.stop_turn()
            await self._persist()
            await self._publish_board()
            await self._publish_clock()

    # ----- view mode -----

    def current_fen(self) -> str | None:
        """Snapshot of the current board's full FEN, or None if no game
        is set up yet. Read-only; does not acquire the lock."""
        return self._board.fen() if self._board is not None else None

    def play_game_snapshot(
        self,
    ) -> tuple[str | None, list[str], list[tuple[float, float]], float, float, list[dict | None]]:
        """Return (start_fen, moves_uci, clock_history, white_time, black_time, eval_history)
        from the current play-mode game. Safe to call without the lock."""
        board = self._board
        moves = [m.uci() for m in board.move_stack] if board else []
        return (
            self._start_fen,
            moves,
            list(self._clock.history),
            self._clock.white_time,
            self._clock.black_time,
            list(self._eval_history),
        )

    def play_game_comments(self) -> tuple[list[str | None] | None, str | None]:
        """Return (comments, root_comment) from the current play-mode game.

        Populated only when the play game was seeded from a view fork
        (play_from_here) that carried commentary. Used by /game/view/start
        so the play -> view clone preserves imported annotations through
        an edit-mode round trip."""
        comments = list(self._play_comments) if self._play_comments is not None else None
        return comments, self._play_root_comment

    @property
    def fork_link(self) -> tuple[str, int] | None:
        """Read-only view of the current fork link, if any. Used by the
        API layer to pass the link across an ``enter_view_mode`` boundary
        when the transition preserves lineage (``/view/start``)."""
        return self._fork_link

    def clear_fork_link(self) -> None:
        """Drop the current fork link. Used by the API layer once the
        link has been consumed by a recents write."""
        self._fork_link = None

    async def enter_view_mode(
        self,
        params: ViewModeParams,
        game_id: str | None = None,
        fork_link: tuple[str, int] | None = None,
        land_at_ply: int | None = None,
    ) -> str:
        """Load a PGN-imported game into view mode at the LAST ply.

        Replaces any active live game (the autosave file preserves it for
        future load-from-history). No clocks tick, no engine thinks, no
        autosave fires. Navigation is via view_first/back/forward/last.
        Exit via play_from_here, which seeds a fresh play game.

        ``game_id`` is an opaque server-side identity for the loaded
        content. When the caller has one (e.g. import path found the
        content already in the store), pass it through so the live
        session and the store agree. When omitted, a fresh uuid4 is
        minted.

        ``fork_link``: optional ``(parent_game_id, fork_ply)``. The
        store is replaced unconditionally with this value -- so the
        default ``None`` drops any stale link from a prior play game
        (import-on-top, FEN-edit commit), and ``/view/start`` passes
        its current link through so it survives the play -> view ->
        edit -> annotate flow.
        """
        async with self._lock:
            if not (self._mode & Op.ENTER_VIEW_MODE._mask):
                raise ModeConflictError(self._mode, Op.ENTER_VIEW_MODE)
            await self._cancel_analysis()
            await self._cancel_think()
            await self._cancel_tick()
            self._ensure_tablebase()
            # Unconditional replacement: default ``None`` drops a stale
            # link from a prior play game (import-on-top, FEN-edit
            # commit); ``/view/start`` passes the current link through
            # so play -> view -> edit -> annotate can record it on
            # commit.
            self._fork_link = fork_link
            try:
                start_board = board_from(params.start_fen)
            except ValueError as e:
                raise RuntimeError(f"invalid FEN: {e}") from e
            full_moves: list[chess.Move] = []
            replay = start_board.copy()
            for uci in params.moves_uci:
                try:
                    move = chess.Move.from_uci(uci)
                except ValueError as e:
                    raise RuntimeError(f"invalid UCI in view moves: {uci}") from e
                if move not in replay.legal_moves:
                    raise RuntimeError(f"illegal move in view: {uci}")
                full_moves.append(move)
                replay.push(move)
            self._mode = Mode.VIEWING
            self._view_full_moves = full_moves
            self._view_clock_history = list(params.clock_history) if params.clock_history else []
            self._view_final_white = params.final_white_time
            self._view_final_black = params.final_black_time
            self._view_white_name = params.white_name
            self._view_black_name = params.black_name
            self._view_pgn_result = params.pgn_result
            self._view_pgn_termination = params.pgn_termination
            self._view_eval_history = list(params.eval_history) if params.eval_history else None
            self._view_comments = list(params.comments) if params.comments else None
            self._view_root_comment = params.root_comment or None
            self._view_hash = params.view_hash or None
            self._view_summary = params.view_summary or None
            self._view_original_text = params.view_original_text or None
            self._view_edited = False
            # Default: land at start so the user is not greeted with the
            # end-of-game modal. Callers (e.g. x-game nav) can request a
            # specific ply so the first published board_update is already
            # at the target position -- avoids an animation flicker when
            # the cursor is then re-targeted from the client.
            if land_at_ply is not None and 0 < land_at_ply <= len(full_moves):
                self._view_cursor = land_at_ply
                replay = start_board.copy()
                for m in full_moves[:land_at_ply]:
                    replay.push(m)
                self._board = replay
            else:
                self._view_cursor = 0
                self._board = start_board
            self._start_fen = params.start_fen
            self._game_id = game_id if game_id is not None else str(uuid.uuid4())
            self._game_started_wall = None  # not a play game; no autosave
            # Clocks frozen -- irrelevant in view mode but keep types sane.
            self._clock = ChessClock(TimeControl(0.0, 0.0))
            await self._publish_board()
            await self._publish_clock()
        return self._game_id

    def _comment_nav(self, cursor: int) -> dict:
        """Return prev/next ply indices (0..n) that have a comment, nearest first."""
        # Build a flat lookup: ply 0 -> root comment, ply i -> _view_comments[i-1].
        def has_comment(ply: int) -> bool:
            if ply == 0:
                return self._view_root_comment is not None
            comments = self._view_comments
            return bool(comments and (i := ply - 1) < len(comments) and comments[i] is not None)

        n = len(self._view_full_moves)
        prev_c = next(
            (i for i in range(cursor - 1, -1, -1) if has_comment(i)), None
        )
        next_c = next(
            (i for i in range(cursor + 1, n + 1) if has_comment(i)), None
        )
        return {"prev_comment": prev_c, "next_comment": next_c}

    async def view_goto(self, ply: int) -> None:
        """Move the view cursor to ``ply`` (0..len(full_moves)). Rebuilds
        the board by replaying from start. Rejected during analysis.
        Comment-nav state is shipped in the resulting board_update payload."""
        async with self._lock:
            if not (self._mode & Op.VIEW_GOTO._mask):
                raise ModeConflictError(self._mode, Op.VIEW_GOTO)
            n = len(self._view_full_moves)
            if ply < 0 or ply > n:
                raise RuntimeError(f"ply out of range: {ply} (0..{n})")
            self._view_cursor = ply
            board = board_from(self._start_fen)
            for m in self._view_full_moves[:ply]:
                board.push(m)
            self._board = board
            await self._publish_board()
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

    async def enter_edit_mode(self) -> str:
        """Enter board editing. Must be in view mode; live play rejects.
        Stops analysis if running. Snapshots the current FEN so cancel
        can restore it. Returns the snapshotted FEN.
        """
        async with self._lock:
            if not (self._mode & Op.ENTER_EDIT_MODE._mask):
                raise ModeConflictError(self._mode, Op.ENTER_EDIT_MODE)
            if self._board is None:
                raise RuntimeError("no position")
            need_cancel_analysis = self._mode is Mode.ANALYZING
            if need_cancel_analysis:
                self._mode = Mode.VIEWING
            pre_fen = self._board.fen()
            self._edit_pre_fen = pre_fen
            self._edit_view_snapshot = _ViewSnapshot(
                start_fen=self._start_fen,
                board=self._board.copy(),
                cursor=self._view_cursor,
                full_moves=list(self._view_full_moves),
                clock_history=list(self._view_clock_history),
                final_white=self._view_final_white,
                final_black=self._view_final_black,
                white_name=self._view_white_name,
                black_name=self._view_black_name,
                eval_history=list(self._view_eval_history) if self._view_eval_history is not None else None,
                comments=list(self._view_comments) if self._view_comments is not None else None,
                root_comment=self._view_root_comment,
                pgn_result=self._view_pgn_result,
                pgn_termination=self._view_pgn_termination,
                view_hash=self._view_hash,
                view_summary=self._view_summary,
                view_original_text=self._view_original_text,
                view_edited=self._view_edited,
            )
            self._mode = Mode.EDITING
        if need_cancel_analysis:
            await self._cancel_analysis()
        async with self._lock:
            await self._publish_board()
        return pre_fen

    def _restore_view_snapshot(self, snap: _ViewSnapshot) -> None:
        """Apply a snapshot taken by enter_edit_mode directly to view state."""
        self._start_fen = snap.start_fen
        self._board = snap.board
        self._view_cursor = snap.cursor
        self._view_full_moves = snap.full_moves
        self._view_clock_history = snap.clock_history
        self._view_final_white = snap.final_white
        self._view_final_black = snap.final_black
        self._view_white_name = snap.white_name
        self._view_black_name = snap.black_name
        self._view_eval_history = snap.eval_history
        self._view_comments = snap.comments
        self._view_root_comment = snap.root_comment
        self._view_pgn_result = snap.pgn_result
        self._view_pgn_termination = snap.pgn_termination
        self._view_hash = snap.view_hash
        self._view_summary = snap.view_summary
        self._view_original_text = snap.view_original_text
        self._view_edited = snap.view_edited

    async def commit_edit(
        self,
        fen: str,
        *,
        apply_comment: bool = False,
        comment_text: str = "",
    ) -> dict:
        """Apply the edited FEN (and optionally an annotation at the
        edit-entry ply) as the next view-mode state. On any failure
        (FEN parse, illegality, replay) leaves edit mode intact so the
        user can fix and retry.

        ``apply_comment=True`` requests an annotation commit at the
        snapshotted entry ply (cursor at enter_edit_mode time). Empty
        ``comment_text`` deletes the annotation. Annotation commit is
        only honored on the FEN-unchanged branch; if the FEN changed
        the game is truncated and the requested annotation has no
        valid target.

        Returns a dict::

            {
                "game_id":   str,
                "changed":   "fen" | "comment" | "none",
                "pgn_text":  str | None,   # set when changed == "comment"
                "hash":      str | None,   # canonical hash for the new PGN
                "summary":   dict | None,  # carry-through of _view_summary
            }

        The API layer uses ``changed`` to dispatch the recents-store
        write (FEN -> save a FEN-only row; comment -> replace_at; none
        -> no recents touch).
        """
        async with self._lock:
            if not (self._mode & Op.COMMIT_EDIT._mask):
                raise ModeConflictError(self._mode, Op.COMMIT_EDIT)
            try:
                board = board_from(fen)
            except ValueError as e:
                raise RuntimeError(f"invalid FEN: {e}") from e
            if not board.is_valid():
                raise RuntimeError(explain_invalid(board))
            target_fen = board.fen()
            pre_epd = board_from(self._edit_pre_fen).epd() if self._edit_pre_fen else None
            unchanged = pre_epd is not None and board.epd() == pre_epd
            snap = self._edit_view_snapshot
            self._mode = Mode.VIEWING
        if unchanged and snap is not None:
            async with self._lock:
                self._restore_view_snapshot(snap)
                self._edit_pre_fen = None
                self._edit_view_snapshot = None
                # Annotation branch: apply requested comment at the
                # entry ply, regen PGN + hash, publish. Reuses the
                # just-restored snapshot as the base state.
                if apply_comment:
                    annot = self._apply_view_annotation(
                        ply=snap.cursor, text=comment_text,
                    )
                else:
                    annot = None
                await self._publish_board()
                await self._publish_clock()
            if annot is not None:
                pgn_text, new_hash = annot
                return {
                    "game_id": self._game_id,
                    "changed": "comment",
                    "pgn_text": pgn_text,
                    "hash": new_hash,
                    "summary": self._view_summary,
                }
            return {
                "game_id": self._game_id,
                "changed": "none",
                "pgn_text": None,
                "hash": None,
                "summary": None,
            }
        # FEN changed -- drop history, enter fresh view at new position.
        try:
            game_id = await self.enter_view_mode(
                ViewModeParams(start_fen=target_fen, moves_uci=[], clock_history=None)
            )
        except Exception:
            async with self._lock:
                self._mode = Mode.EDITING
            raise
        async with self._lock:
            self._edit_pre_fen = None
            self._edit_view_snapshot = None
        return {
            "game_id": game_id,
            "changed": "fen",
            "pgn_text": None,
            "hash": None,
            "summary": None,
        }

    def _apply_view_annotation(
        self, *, ply: int, text: str,
    ) -> tuple[str, str] | None:
        """Mutate view-state to set/clear the comment at ``ply`` (0 ==
        root, N == comment after move N), regen the PGN + canonical
        hash, update ``_view_hash``. Returns ``(pgn_text, new_hash)``
        or None when the requested annotation matches what's already
        there (no-op).

        Caller MUST hold the lock. ``_view_original_text`` is intentionally
        NOT touched -- it stays the original import bytes.
        """
        assert self._lock.locked(), "_apply_view_annotation called without lock"
        new_value = text.strip() or None
        if ply == 0:
            current = self._view_root_comment
            if current == new_value:
                return None
            self._view_root_comment = new_value
        else:
            n = len(self._view_full_moves)
            if not (0 < ply <= n):
                raise RuntimeError(f"ply {ply} out of range [0, {n}]")
            idx = ply - 1
            if self._view_comments is None:
                if new_value is None:
                    return None  # nothing to clear; was already absent
                self._view_comments = [None] * n
            current = self._view_comments[idx]
            if current == new_value:
                return None
            self._view_comments[idx] = new_value
            # Collapse to None if no comments remain anywhere.
            if all(c is None for c in self._view_comments):
                self._view_comments = None
        built = self._build_view_pgn()
        if built is None:
            return None
        pgn_text, _w, _b = built
        new_hash = canonical_hash(pgn_text, "pgn")
        self._view_hash = new_hash
        self._view_edited = True
        return pgn_text, new_hash

    async def cancel_edit(self) -> str:
        """Leave edit mode; restore view state from pre-edit snapshot."""
        async with self._lock:
            if not (self._mode & Op.CANCEL_EDIT._mask):
                raise ModeConflictError(self._mode, Op.CANCEL_EDIT)
            snap = self._edit_view_snapshot
            assert snap is not None, "enter_edit_mode always sets _edit_view_snapshot"
            self._mode = Mode.VIEWING
            self._restore_view_snapshot(snap)
            self._edit_pre_fen = None
            self._edit_view_snapshot = None
            await self._publish_board()
            await self._publish_clock()
        return self._game_id

    async def play_from_here(
        self,
        tc: TimeControl,
        inherit_clocks: bool = False,
        player_name: str | None = None,
    ) -> str:
        """Exit view mode by starting a fresh play game seeded with plies
        0..cursor. New game_id, new autosave file. Side-to-play is whoever
        is to move at the cursor (matches today's import default).

        ``inherit_clocks``: when True, seed live clocks from the PGN cursor
        (study time pressure / repro engine behavior). When False, live
        clocks reset to ``tc.initial_seconds``.
        """
        async with self._lock:
            if not (self._mode & Op.PLAY_FROM_HERE._mask):
                raise ModeConflictError(self._mode, Op.PLAY_FROM_HERE)
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
            # Snapshot view-mode commentary slice before _reset_view_state wipes it.
            seed_comments = (
                list(self._view_comments[:cursor])
                if self._view_comments is not None
                else None
            )
            seed_root_comment = self._view_root_comment
            start_fen = self._start_fen
            # Determine side-to-move at the cursor without leaving the lock.
            board = board_from(start_fen)
            for m in self._view_full_moves[:cursor]:
                board.push(m)
            # Refuse if the cursor lands on a finished position — would
            # otherwise raise inside new_game AFTER viewer state is cleared,
            # stranding the user in neither view nor play. Caller should
            # nav back first (UI disables the button at game-over plies).
            if board.is_game_over():
                raise RuntimeError("game is over at this ply; back up first")
            human_white = (board.turn == chess.WHITE)
            # Capture fork link before new_game wipes it. The parent's
            # game_id is the *current* self._game_id (we are still in
            # view mode pointing at it). fork_ply==0 is a degenerate
            # fork -- treated as a plain new game with no link.
            parent_game_id = self._game_id
            fork_ply = cursor
            # Exit view mode before the new_game call (which re-acquires
            # the lock). Clear viewer state so new_game starts clean.
            self._mode = Mode.PLAY
            self._reset_view_state()
        new_id = await self.new_game(
            human_white=human_white,
            tc=tc,
            player_name=player_name or self._player_name,
            start_fen=start_fen,
            start_moves_uci=seed_moves,
            seed_clock_history=seed_clocks,
            seed_final_white_time=seed_final_w,
            seed_final_black_time=seed_final_b,
            seed_comments=seed_comments,
            seed_root_comment=seed_root_comment,
        )
        # Re-stash the fork link after new_game cleared it. Only when
        # parent_id is known AND fork_ply >= 1 (ply 0 fork == plain new
        # game, no link).
        if parent_game_id is not None and fork_ply >= 1:
            self._fork_link = (parent_game_id, fork_ply)
        return new_id

    async def apply_engine_settings_live(self) -> None:
        """Force the play engine to respawn so the latest options/args/env
        and global defaults take effect on the next move.

        Skipped during analysis -- the analysis engine is a throwaway and
        respawns per session anyway; clobbering it would interrupt the user.
        Caller is expected to have already updated _engine_options /
        _engine_args / _engine_env via the setters; this just discards the
        live process so _ensure_engine respawns with the new layering.
        """
        kick_engine = False
        async with self._lock:
            if self._analysis_mode:
                return
            await self._cancel_think()
            await self._quit_engine()
            if (
                self._clock_running
                and self._game_id is not None
                and self._board.turn == self._engine_color()
            ):
                kick_engine = True
        if kick_engine:
            await self._engine_to_move()

    async def swap_engine(self, path: str) -> None:
        """Replace the engine binary; preserves the active game.

        Per-engine UCI options / args / env from the previous binary are
        cleared so they don't leak into the new spawn. The API layer
        re-seeds them from the registry on the next fetch.
        """
        kick_engine = False
        async with self._lock:
            await self._cancel_analysis()
            if self._mode is Mode.ANALYZING:
                self._mode = self._pre_analysis_mode
            await self._cancel_think()
            await self._supervisor.swap(path)
            if self._board is not None and self._game_id is not None:
                await self._publish_board()
                if self._clock_running and self._board.turn == self._engine_color():
                    kick_engine = True
        if kick_engine:
            await self._engine_to_move()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._cancel_analysis()
            if self._mode is Mode.ANALYZING:
                self._mode = self._pre_analysis_mode
            await self._cancel_think()
            await self._cancel_tick()
            await self._quit_engine()
            if self._tb is not None:
                self._tb.close()
                self._tb = None

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
            tc_initial_seconds=self._clock.tc.initial_seconds,
            tc_increment_seconds=self._clock.tc.increment_seconds,
            white_time=self._clock.white_time,
            black_time=self._clock.black_time,
            paused=self._paused,
            moves_uci=[m.uci() for m in self._board.move_stack],
            clock_history=[[w, b] for (w, b) in self._clock.history],
            eval_history=list(self._eval_history),
            start_fen=self._start_fen,
            game_started_wall=self._game_started_wall,
            player_name=self._player_name,
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
        self._board = board_from(state.start_fen)
        self._start_fen = state.start_fen
        for uci in state.moves_uci:
            self._board.push(chess.Move.from_uci(uci))
        self._game_id = state.game_id
        # Fall back to now() for older saves missing this field — preserves
        # autosave behavior, just renames the file going forward.
        self._game_started_wall = state.game_started_wall or time.time()
        self._human_white = state.human_white
        self._player_name = state.player_name or DEFAULT_PLAYER_NAME
        self._clock = ChessClock(TimeControl(
            initial_seconds=state.tc_initial_seconds,
            increment_seconds=state.tc_increment_seconds,
        ))
        self._clock.white_time = state.white_time
        self._clock.black_time = state.black_time
        self._clock.history = [(w, b) for (w, b) in state.clock_history]
        # Marker: turn hasn't started ticking yet. republish_state() sets it.
        self._clock.stop_turn()
        n_plies = len(self._board.move_stack)
        if len(state.eval_history) == n_plies:
            self._eval_history = list(state.eval_history)
        else:
            # Older save (pre-eval_history persistence) or schema drift: fall
            # back to all-None so the per-ply invariant holds and subsequent
            # engine searches can still extend the list.
            self._eval_history = [None] * n_plies
        self._mode = Mode.PAUSED if state.paused else Mode.PLAY

    # ----- internals -----

    def _consume_turn_time(self) -> None:
        """Debit elapsed from side-just-moved (board.turn pre-push), add increment."""
        if self._board is None:
            return
        self._clock.consume_turn(self._board.turn)

    def _remaining(self, side: chess.Color) -> float:
        """Live remaining for `side`, ticking down only on its turn."""
        if self._board is None:
            return self._clock.white_time if side == chess.WHITE else self._clock.black_time
        return self._clock.remaining(
            side,
            stm=self._board.turn,
            game_over=self._paused or self._board.is_game_over(),
        )

    async def _cancel_think(self) -> None:
        """Stop the current search; keep the engine alive for reuse.

        Tears down the transport only when the engine fails to acknowledge
        `stop` within the grace period (wedged/crashed). Callers that need
        the subprocess gone (swap, shutdown) must follow up with
        `_quit_engine`.
        """
        self._think_gen += 1
        think_task = self._think_task
        analysis = self._analysis
        self._think_task = None
        self._analysis = None
        await self._supervisor.cancel(think_task=think_task, analysis=analysis)

    async def _quit_engine(self) -> None:
        """Gracefully terminate the engine subprocess. Call after `_cancel_think`."""
        await self._supervisor.quit()

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
        self._last_analysis_info = None

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
            loser = side_to_move(self._board)
            game_id = self._game_id
            await self._cancel_think()
            # Loser is the side to move when the flag fell.
            result = loser_result(loser == "white")
            self._maybe_save_pgn(result=result, termination="time_forfeit")
            self._stash_recents_payload(result=result, termination="time_forfeit")
        await self._bus.publish(
            Event(
                kind="game_result",
                game_id=game_id,
                payload={"result": "timeout", "loser": loser},
            )
        )
        await self._flush_recents_save()
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

    async def _pump_engine_info(
        self,
        analysis,
        game_id: str,
        board: chess.Board,
        cache_payload: bool = False,
        capture_score: dict | None = None,
    ) -> None:
        """Drain analysis info events, serialize + publish, optionally cache.

        Thin wrapper that pins HVE-specific behavior (eval POV honoring
        `play_eval_pov`, last-payload cache for /game/sync replay) over
        the shared `pump_engine_info` loop.
        """
        def _cache(payload: dict) -> None:
            self._last_analysis_info = payload
        await pump_engine_info(
            analysis,
            bus=self._bus,
            game_id=game_id,
            board=board,
            pov=self._eval_pov(board.turn),
            on_payload=_cache if cache_payload else None,
            capture_score=capture_score,
        )  # cancel handled via asyncio task cancellation, not cancel_token

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
            white_inc=self._clock.tc.increment_seconds,
            black_inc=self._clock.tc.increment_seconds,
        )
        # Clear the live engine panel at search start; real info events will
        # repopulate it. Book moves return bestmove without info, leaving it
        # blank -- which is the signal we want.
        await self._bus.publish(
            Event(kind="engine_search_start", game_id=game_id, payload={})
        )
        captured: dict = {}
        try:
            with await engine.analysis(board, limit=limit) as analysis:
                self._analysis = analysis
                await self._pump_engine_info(analysis, game_id, board, capture_score=captured)
                result = analysis.wait()  # returns BestMove
                best_move = await result
                best = best_move.move
                if best is None:
                    return
        except chess.engine.EngineTerminatedError:
            if self._engine is engine:
                self._engine = None
            if self._think_gen == gen:
                log.error("engine crashed mid-search")
                await self._cancel_tick()
                await self._bus.publish(
                    Event(kind="system", game_id=game_id, payload={"error": "engine_terminated"})
                )
            else:
                log.info("engine terminated (takeback or shutdown)")
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
            self._clock.append_snapshot()
            self._consume_turn_time()
            self._board.push(best)
            self._eval_history.append(captured or None)
            if self._play_comments is not None:
                self._play_comments.append(None)
            await self._persist()
            await self._publish_board()
            await self._publish_clock()
            ended = self._is_ended()
            if ended:
                end_game_id, end_payload = self._finalize_game_locked()
            else:
                self._maybe_save_pgn(result="*", termination="unterminated")
        if ended:
            await self._cancel_tick()
            await self._bus.publish(
                Event(kind="game_result", game_id=end_game_id, payload=end_payload)
            )
            await self._flush_recents_save()

    async def _run_analysis(self, game_id: str, board: chess.Board) -> None:
        """Drive analysis on a dedicated engine instance.

        A throwaway process avoids re-configuring + reverting Threads on the
        play engine (and any state bleed it could cause). Killed on exit.
        Spawn + settings-derived options are owned by the shared
        engine-analysis helper so this stays in sync with the AI tool.
        """
        try:
            engine, cleanup = await spawn_analysis_engine(
                self._supervisor, self._settings,
            )
        except Exception:
            log.exception("could not start engine for analysis")
            return
        await self._bus.publish(
            Event(kind="engine_search_start", game_id=game_id, payload={})
        )
        try:
            with await engine.analysis(board) as analysis:
                self._analysis = analysis
                await self._pump_engine_info(analysis, game_id, board, cache_payload=True)
        except chess.engine.EngineTerminatedError:
            log.error("engine crashed mid-analysis")
            await self._bus.publish(
                Event(kind="system", game_id=game_id, payload={"error": "engine_terminated"})
            )
        except (asyncio.CancelledError, RuntimeError, BrokenPipeError):
            return
        finally:
            self._analysis = None
            # cleanup() is documented to swallow quit/pipe errors;
            # no surrounding try needed (and the previous narrow tuple
            # missed OSError from Windows proactor on pipe close).
            await cleanup()

    def _opening_payload(self) -> dict | None:
        """Opening-book lookup. Keys on the standard starting position; an
        imported (non-startpos) game can't be classified."""
        if (
            self._openings is None
            or not self._board.move_stack
            or self._start_fen is not None
        ):
            return None
        ucis = [m.uci() for m in self._board.move_stack]
        hit = self._openings.lookup(ucis)
        return {"eco": hit.eco, "name": hit.name} if hit is not None else None

    def _view_moves_san(self) -> list[str]:
        """Full-game SAN list for view mode (the UI highlights one ply via
        cursor; in play mode the moves come straight off the live board)."""
        full_board = board_from(self._start_fen)
        out: list[str] = []
        for m in self._view_full_moves:
            out.append(full_board.san(m))
            full_board.push(m)
        return out

    def _view_payload(self) -> dict:
        """Build the per-event view-mode payload (cursor, eval, comment,
        game_over, result/termination). Only called when self._viewing is
        True; reads only view-mode attrs + self._board."""
        # Eval at the cursor = eval recorded for the last played move.
        # cursor==0 means initial position, no move yet -> no eval.
        eval_at_cursor = None
        if (
            self._view_eval_history is not None
            and 0 < self._view_cursor <= len(self._view_eval_history)
        ):
            eval_at_cursor = self._view_eval_history[self._view_cursor - 1]
        has_any_eval = (
            self._view_eval_history is not None
            and any(e is not None for e in self._view_eval_history)
        )
        comment_at_cursor: str | None = None
        if self._view_cursor == 0:
            comment_at_cursor = self._view_root_comment
        elif (
            self._view_comments is not None
            and 0 < self._view_cursor <= len(self._view_comments)
        ):
            comment_at_cursor = self._view_comments[self._view_cursor - 1]
        has_any_comment = (
            self._view_root_comment is not None
            or (
                self._view_comments is not None
                and any(c is not None for c in self._view_comments)
            )
        )
        outcome = self._board.outcome()
        # UI disables Play-from-here when the cursor lands on a finished
        # position (mirror of the backend guard). game_over is true for
        # forced endings AND claimable draws recorded in the PGN headers.
        game_over = outcome is not None or (
            bool(self._view_pgn_result)
            and self._view_pgn_result != "*"
            and self._view_cursor == len(self._view_full_moves)
        )
        if outcome is not None:
            result_termination = {
                "result": outcome.result(),
                "termination": outcome.termination.name.lower(),
            }
        elif self._view_pgn_result and self._view_pgn_result != "*":
            result_termination = {
                "result": self._view_pgn_result,
                "termination": (
                    "threefold_repetition"
                    if self._board.can_claim_threefold_repetition()
                    else "fifty_moves"
                    if self._board.can_claim_fifty_moves()
                    else self._view_pgn_termination
                ) if self._view_pgn_termination == "normal" else self._view_pgn_termination,
            }
        else:
            result_termination = {}
        return {
            "cursor": self._view_cursor,
            "total_plies": len(self._view_full_moves),
            "white_name": self._view_white_name,
            "black_name": self._view_black_name,
            "eval": eval_at_cursor,
            "has_eval": has_any_eval,
            "comment": comment_at_cursor,
            "has_comment": has_any_comment,
            "game_over": game_over,
            "view_hash": self._view_hash,
            "view_summary": self._view_summary,
            **self._comment_nav(self._view_cursor),
            **result_termination,
        }

    def _board_event(self) -> Event:
        assert self._board is not None and self._game_id is not None
        if self._viewing:
            moves_san = self._view_moves_san()
            view_payload = self._view_payload()
        else:
            moves_san = _moves_san(self._board, self._start_fen)
            view_payload = None
        return Event(
            kind="board_update",
            game_id=self._game_id,
            payload={
                "fen": self._board.fen(),
                "turn": side_to_move(self._board),
                "ply": self._board.ply(),
                "moves_san": moves_san,
                "last_move": self._board.peek().uci() if self._board.move_stack else None,
                # human_white is meaningless in view mode (the user isn't
                # playing); omit so the UI's local flip isn't clobbered.
                "human_white": None if self._viewing else self._human_white,
                "engine_name": self._engine_name,
                "player_name": self._player_name,
                "opening": self._opening_payload(),
                "tablebase": {
                    "halfmove_clock": self._board.halfmove_clock,
                    **(self._tb.probe(self._board) or {} if self._tb else {}),
                },
                "analyzing": self._analysis_mode,
                "editing": self._editing,
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
                    "turn": side_to_move(self._board),
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
                "turn": side_to_move(self._board),
                "running": self._clock_running,
                "paused": self._paused,
                "analyzing": self._analysis_mode,
            },
        )

    def _is_ended(self) -> bool:
        """True if the game is over, including claimable draws when auto_claim_draws is on."""
        assert self._board is not None
        if self._board.is_game_over():
            return True
        if getattr(self._settings, "auto_claim_draws", True):
            return (
                self._board.can_claim_threefold_repetition()
                or self._board.can_claim_fifty_moves()
            )
        return False

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
        if outcome:
            result = outcome.result()
            termination = outcome.termination.name.lower()
        elif self._board.can_claim_threefold_repetition():
            result, termination = DRAW, "threefold_repetition"
        elif self._board.can_claim_fifty_moves():
            result, termination = DRAW, "fifty_moves"
        else:
            result, termination = "*", "unknown"
        self._maybe_save_pgn(result=result, termination=termination)
        self._stash_recents_payload(result=result, termination=termination)
        self._clear_store()
        game_id = self._game_id
        self._game_id = None
        self._board = None
        return game_id, {"result": result, "termination": termination}

    def _make_pgn_filename(self, white: str, black: str) -> str:
        wall = self._game_started_wall or time.time()
        ts = datetime.datetime.fromtimestamp(wall).strftime("%Y%m%d-%H%M%S")
        w_safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in white)
        b_safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in black)
        return f"sturddle-{ts}-{w_safe}-vs-{b_safe}.pgn"

    def _build_play_game_pgn(
        self, *, result: str, termination: str,
    ) -> tuple[str, str, str] | None:
        """Build a play-mode PGN from the live board, headers, opening,
        time control, clocks, and eval history. Returns (pgn_text,
        white, black) or None when there's nothing to build (no game or
        no moves). Caller is responsible for the lock when serializing
        with concurrent mutators.

        Shared by ``get_pgn_text`` (download), ``_maybe_save_pgn`` (disk
        autosave), and ``_stash_recents_payload`` (recent-imports save).
        """
        if self._board is None or self._game_id is None:
            return None
        if not self._board.move_stack:
            return None
        engine_label = self._engine_name or Path(self._engine_path).name
        white = self._player_name if self._human_white else engine_label
        black = engine_label if self._human_white else self._player_name
        headers = {
            "Event": "Sturddle View -- Human vs Engine",
            "Site": "Sturddle View",
            "Date": datetime.date.today().strftime("%Y.%m.%d"),
            "White": white,
            "Black": black,
        }
        opening = None
        if self._openings is not None and self._start_fen is None:
            ucis = [m.uci() for m in self._board.move_stack]
            hit = self._openings.lookup(ucis)
            if hit is not None:
                opening = (hit.eco, hit.name)
        tc = None
        if self._clock.tc.initial_seconds:
            tc = (int(self._clock.tc.initial_seconds), int(self._clock.tc.increment_seconds))
        # Comments slice must match move_stack length. _play_comments is
        # maintained in lockstep with the board by new_game (seed) and
        # take_back (pop), so just clip defensively in case the lengths
        # ever drift.
        n_plies = len(self._board.move_stack)
        comments = (
            list(self._play_comments[:n_plies])
            if self._play_comments is not None
            else None
        )
        pgn_text = build_pgn(
            start_fen=self._start_fen,
            moves_uci=[m.uci() for m in self._board.move_stack],
            clock_history=list(self._clock.history),
            final_clocks=(self._clock.white_time, self._clock.black_time),
            headers=headers,
            opening=opening,
            result=result,
            termination=termination,
            time_control=tc,
            eval_history=list(self._eval_history),
            comments=comments,
            root_comment=self._play_root_comment,
        )
        return pgn_text, white, black

    def get_pgn_text(self) -> tuple[str, str] | None:
        """Return (pgn_text, suggested_filename) for the current game, or None.

        Covers play mode (in-progress or finished) and view mode after a PGN
        import or a play_from_here fork.  Returns None when there is nothing
        to export (no board, no moves, or FEN-only view with no game history).
        """
        if self._board is None:
            return None

        if self._viewing and self._view_original_text and not self._view_edited:
            # Verbatim round-trip: return original import bytes unchanged.
            # Covers zero-move PGNs (headers-only) as well as full games.
            # Skipped when structured state has diverged from the import
            # (e.g. annotation edit) -- fall through to _build_view_pgn.
            white = self._view_white_name or "?"
            black = self._view_black_name or "?"
            return self._view_original_text, self._make_pgn_filename(white, black)

        if self._viewing and not self._view_full_moves:
            return None  # FEN-only view with no verbatim text: nothing to export

        if self._viewing:
            built = self._build_view_pgn()
            if built is None:
                return None
            pgn_text, white, black = built
        else:
            built = self._build_play_game_pgn(result="*", termination="unterminated")
            if built is None:
                return None
            pgn_text, white, black = built

        return pgn_text, self._make_pgn_filename(white, black)

    def _build_view_pgn(self) -> tuple[str, str, str] | None:
        """Assemble a PGN from the live view-mode state. Returns
        (pgn_text, white, black) or None when there's nothing to build
        (no moves). Shared by ``get_pgn_text`` (download when no raw
        text is available) and ``commit_edit`` (annotation save: regen
        from the post-edit comment array).

        Always passes ``comments`` and ``root_comment`` to ``build_pgn``
        so user prose round-trips. Caller is responsible for holding
        the lock when serializing with concurrent mutators.
        """
        if not self._view_full_moves:
            return None
        result = self._view_pgn_result or "*"
        termination = self._view_pgn_termination or "unterminated"
        white = self._view_white_name or "?"
        black = self._view_black_name or "?"
        # "Sturddle View" (no player label) -- origin unknown after fork/rebuild.
        headers = {
            "Event": "Sturddle View",
            "Site": "Sturddle View",
            "Date": datetime.date.today().strftime("%Y.%m.%d"),
            "White": white,
            "Black": black,
        }
        view_evals = (
            list(self._view_eval_history)
            if self._view_eval_history is not None
            else [None] * len(self._view_full_moves)
        )
        n_plies = len(self._view_full_moves)
        comments = (
            list(self._view_comments[:n_plies])
            if self._view_comments is not None
            else None
        )
        pgn_text = build_pgn(
            start_fen=self._start_fen,
            moves_uci=[m.uci() for m in self._view_full_moves],
            clock_history=list(self._view_clock_history) or None,
            final_clocks=(
                (self._view_final_white, self._view_final_black)
                if self._view_final_white is not None and self._view_final_black is not None
                else None
            ),
            headers=headers,
            result=result,
            termination=termination,
            eval_history=view_evals,
            comments=comments,
            root_comment=self._view_root_comment,
        )
        return pgn_text, white, black

    def _maybe_save_pgn(self, *, result: str, termination: str) -> Path | None:
        if self._board is None or self._game_id is None:
            return None
        if self._viewing:
            return None
        if self._settings is None or not getattr(self._settings, "pgn_autosave", False):
            return None
        if not self._board.move_stack:
            return None

        raw = getattr(self._settings, "pgn_dir", None)
        if not raw:
            return None
        pgn_dir = Path(raw).expanduser()
        try:
            pgn_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("could not create PGN dir %s", pgn_dir)
            return None

        built = self._build_play_game_pgn(result=result, termination=termination)
        if built is None:
            return None
        pgn_text, _white, _black = built

        # Game-start timestamp keeps the path stable across per-move autosaves
        # and the final end-of-game write, so the file is overwritten in place.
        wall = self._game_started_wall or time.time()
        ts = datetime.datetime.fromtimestamp(wall).strftime("%Y%m%d-%H%M%S")
        path = pgn_dir / f"{ts}-{self._game_id}.pgn"
        try:
            atomic_write_text(path, pgn_text)
        except OSError:
            log.exception("could not write PGN to %s", path)
            return None
        log.info("saved PGN to %s", path)
        return path

    def _stash_recents_payload(self, *, result: str, termination: str) -> None:
        """Build the finished-game PGN + summary and stash on
        ``_pending_recents_save`` for the post-lock async flush.

        Caller MUST hold ``self._lock``. No-op when there's no recents
        store wired, no active game, no moves, or no game_id. Tagged
        with ``summary["source"]="play"`` so the UI can distinguish
        these from user-initiated imports.
        """
        assert self._lock.locked(), "_stash_recents_payload called without lock"
        if self._recents is None:
            return
        try:
            built = self._build_play_game_pgn(result=result, termination=termination)
        except Exception:
            log.exception("could not build PGN for recents save")
            return
        if built is None:
            return
        pgn_text, white, black = built
        summary = self._play_summary(white=white, black=black, result=result)
        self._pending_recents_save = (pgn_text, summary, self._game_id)

    def _play_summary(
        self, *, white: str, black: str, result: str | None,
    ) -> dict:
        return {
            "white": white,
            "black": black,
            "result": result,
            "side_to_move": "white" if self._board.turn == chess.WHITE else "black",
            "source": "play",
        }

    def play_game_summary(self) -> dict | None:
        """Minimal summary dict for the in-flight play game, matching the
        shape stashed for recents on game-end. Returns None when there's
        no live play game with moves. Used by /game/view/start to label
        the play->view clone."""
        if self._board is None or self._game_id is None or not self._board.move_stack:
            return None
        engine_label = self._engine_name or Path(self._engine_path).name
        white = self._player_name if self._human_white else engine_label
        black = engine_label if self._human_white else self._player_name
        return self._play_summary(white=white, black=black, result=None)

    async def _flush_recents_save(self) -> None:
        """Drain the stash set by _stash_recents_payload. Call AFTER
        releasing self._lock. Best-effort: a failure here must not
        block game-end signaling.

        Also drains ``self._fork_link`` (set by play_from_here) so the
        recents row records the parent_game_id + fork_ply. The link
        is consumed regardless of whether the payload exists -- if a
        finalization arrives without a payload (no moves played), the
        link is dropped on the floor and never establishes."""
        payload = self._pending_recents_save
        self._pending_recents_save = None
        fork_link = self._fork_link
        self._fork_link = None
        if payload is None or self._recents is None:
            return
        text, summary, game_id = payload
        parent_game_id, fork_ply = (
            fork_link if fork_link is not None else (None, None)
        )
        # User-driven export_to_recents may have already written an
        # in-progress row for this game_id. Use replace_at when a row
        # exists so the final PGN swaps in atomically (no game_id-
        # collision crash); fall through to save() for the common
        # "no prior export" case.
        old_hash = self._recents.hash_for_id(game_id)
        try:
            if old_hash is None:
                await self._recents.save(
                    fmt="pgn", text=text, summary=summary, game_id=game_id,
                    parent_game_id=parent_game_id, fork_ply=fork_ply,
                )
            else:
                await self._recents.replace_at(
                    old_hash=old_hash, fmt="pgn",
                    text=text, summary=summary, game_id=game_id,
                    parent_game_id=parent_game_id, fork_ply=fork_ply,
                )
        except Exception:
            log.exception("could not save finished game to recents")

    async def export_to_recents(self) -> str | None:
        """User-driven save: write the in-progress play game to recents
        in addition to whatever download the caller does.

        Update-in-place semantics: if a row for this ``game_id`` already
        exists, ``replace_at`` swaps in the latest content (the same
        path used by edit-commit annotations). Avoids the collision the
        eventual game-end auto-save would otherwise hit.

        Carries the stashed fork link so a forked play game saved
        before finalization still records its parent_game_id + fork_ply.
        The link is consumed on success only; on write failure it stays
        live so a later finalization can still establish it.

        Returns the new hash, or ``None`` when there is nothing to save
        (no game, no moves, no recents store). View-mode games are
        already in recents -- nothing to do here.
        """
        log.info("xgame.export_to_recents called (mode=%s)", self._mode)
        if self._recents is None:
            log.info("xgame.export_to_recents: no recents wired")
            return None
        async with self._lock:
            # Save PGN pauses to freeze state and clock; accept PLAY
            # and PAUSED. View-mode games are already in recents.
            if self._mode not in (Mode.PLAY, Mode.PAUSED):
                log.info(
                    "xgame.export_to_recents: skip (mode=%s, not PLAY/PAUSED)",
                    self._mode,
                )
                return None
            built = self._build_play_game_pgn(
                result="*", termination="unterminated",
            )
            if built is None:
                log.info("xgame.export_to_recents: build_pgn returned None")
                return None
            pgn_text, white, black = built
            summary = self._play_summary(white=white, black=black, result=None)
            game_id = self._game_id
            fork_link = self._fork_link
        log.info(
            "xgame.export_to_recents proceeding game_id=%s fork_link=%s",
            game_id, fork_link,
        )
        # Outside the lock: hit the recents store.
        parent_game_id, fork_ply = (
            fork_link if fork_link is not None else (None, None)
        )
        old_hash = self._recents.hash_for_id(game_id)
        try:
            if old_hash is None:
                result = await self._recents.save(
                    fmt="pgn", text=pgn_text, summary=summary,
                    game_id=game_id,
                    parent_game_id=parent_game_id, fork_ply=fork_ply,
                )
            else:
                result = await self._recents.replace_at(
                    old_hash=old_hash, fmt="pgn",
                    text=pgn_text, summary=summary, game_id=game_id,
                    parent_game_id=parent_game_id, fork_ply=fork_ply,
                )
        except Exception:
            log.exception("could not export play game to recents")
            return None
        # Consume the link only after the write succeeded; on failure
        # leave it in place so a later finalization can still record it.
        async with self._lock:
            if self._fork_link == fork_link:
                self._fork_link = None
        return result
