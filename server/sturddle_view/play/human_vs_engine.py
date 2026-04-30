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

import chess
import chess.engine
import chess.pgn

from ..events import Event, EventBus

log = logging.getLogger(__name__)

CLOCK_TICK_INTERVAL = 0.25  # seconds


@dataclass
class TimeControl:
    initial_seconds: float
    increment_seconds: float = 0.0


def _serialize_info(info: chess.engine.InfoDict, board: chess.Board) -> dict:
    out: dict = {}
    if "depth" in info:
        out["depth"] = info["depth"]
    if "nodes" in info:
        out["nodes"] = info["nodes"]
    if "nps" in info:
        out["nps"] = info["nps"]
    if "time" in info:
        out["time"] = info["time"]
    score = info.get("score")
    if score is not None:
        pov = score.white()
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


def _moves_san(board: chess.Board) -> list[str]:
    """Return the current move stack as SAN strings."""
    if not board.move_stack:
        return []
    replay = chess.Board()
    out = []
    for m in board.move_stack:
        out.append(replay.san(m))
        replay.push(m)
    return out


class HumanVsEngine:
    """Single-game driver. Holds one active game at a time."""

    def __init__(self, engine_path: str, bus: EventBus, openings=None, settings=None) -> None:
        self._engine_path = engine_path
        self._bus = bus
        self._openings = openings  # Optional[OpeningBook]
        self._settings = settings  # Optional[Settings]
        self._engine: chess.engine.UciProtocol | None = None
        self._board: chess.Board | None = None
        self._game_id: str | None = None
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
        self._lock = asyncio.Lock()

    @property
    def engine_path(self) -> str:
        return self._engine_path

    async def _ensure_engine(self) -> chess.engine.UciProtocol:
        if self._engine is None:
            _transport, engine = await chess.engine.popen_uci(self._engine_path)
            # Pre-attach a swallow on the returncode future so an unexpected
            # death (e.g. when we hard-kill the transport) doesn't surface
            # as "Future exception was never retrieved".
            rc_future = getattr(engine, "returncode", None)
            if rc_future is not None:
                rc_future.add_done_callback(lambda f: f.exception())
            self._engine = engine
        return self._engine

    async def new_game(self, human_white: bool, tc: TimeControl) -> str:
        async with self._lock:
            await self._cancel_think()
            await self._cancel_tick()
            engine = await self._ensure_engine()
            engine.send_line("ucinewgame")
            self._board = chess.Board()
            self._human_white = human_white
            self._tc = tc
            self._white_time = tc.initial_seconds
            self._black_time = tc.initial_seconds
            self._turn_started_at = time.monotonic()
            self._clock_history = []
            self._game_id = uuid.uuid4().hex[:12]
            await self._publish_board()
            await self._publish_clock()
        self._start_tick()
        if not human_white:
            await self._engine_to_move()
        return self._game_id

    async def submit_move(self, uci: str) -> None:
        async with self._lock:
            if self._board is None or self._game_id is None:
                raise RuntimeError("no active game")
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
            await self._publish_board()
            await self._publish_clock()
            ended = self._board.is_game_over()
        if ended:
            await self._cancel_tick()
            await self._publish_result()
        else:
            await self._engine_to_move()

    async def republish_state(self) -> None:
        """Re-emit the current board + clock so a stale client can resync."""
        async with self._lock:
            if self._board is None or self._game_id is None:
                return
            await self._publish_board()
            await self._publish_clock()

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
            await self._publish_board()
            await self._publish_clock()

    async def resign(self) -> None:
        async with self._lock:
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

    async def shutdown(self) -> None:
        async with self._lock:
            await self._cancel_think()
            await self._cancel_tick()
            if self._engine is not None:
                try:
                    await self._engine.quit()
                except (chess.engine.EngineTerminatedError, RuntimeError, BrokenPipeError):
                    pass
                self._engine = None

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
        await self._bus.publish(
            Event(
                kind="game_result",
                game_id=game_id,
                payload={"result": "timeout", "loser": loser},
            )
        )
        async with self._lock:
            self._game_id = None
            self._board = None

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
                                payload=_serialize_info(info, board),
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
            await self._publish_board()
            await self._publish_clock()
            ended = self._board.is_game_over()
        if ended:
            await self._cancel_tick()
            await self._publish_result()

    def _board_event(self) -> Event:
        assert self._board is not None and self._game_id is not None
        opening_payload = None
        if self._openings is not None and self._board.move_stack:
            ucis = [m.uci() for m in self._board.move_stack]
            hit = self._openings.lookup(ucis)
            if hit is not None:
                opening_payload = {"eco": hit.eco, "name": hit.name}
        return Event(
            kind="board_update",
            game_id=self._game_id,
            payload={
                "fen": self._board.fen(),
                "turn": "white" if self._board.turn else "black",
                "ply": self._board.ply(),
                "moves_san": _moves_san(self._board),
                "last_move": self._board.peek().uci() if self._board.move_stack else None,
                "human_white": self._human_white,
                "opening": opening_payload,
                "tablebase": None,
            },
        )

    def _clock_event(self) -> Event:
        assert self._board is not None and self._game_id is not None
        return Event(
            kind="clock_tick",
            game_id=self._game_id,
            payload={
                "white_time": self._remaining(chess.WHITE),
                "black_time": self._remaining(chess.BLACK),
                "turn": "white" if self._board.turn else "black",
                "running": not self._board.is_game_over(),
            },
        )

    async def _publish_board(self) -> None:
        await self._bus.publish(self._board_event())

    async def _publish_clock(self) -> None:
        if self._game_id is None or self._board is None:
            return
        await self._bus.publish(self._clock_event())

    async def _publish_result(self) -> None:
        assert self._board is not None and self._game_id is not None
        outcome = self._board.outcome()
        result = outcome.result() if outcome else "*"
        termination = outcome.termination.name.lower() if outcome else "unknown"
        self._maybe_save_pgn(result=result, termination=termination)
        payload = {"result": result, "termination": termination}
        await self._bus.publish(
            Event(kind="game_result", game_id=self._game_id, payload=payload)
        )

    def _maybe_save_pgn(self, *, result: str, termination: str) -> Path | None:
        if self._board is None or self._game_id is None:
            return None
        if self._settings is None or not getattr(self._settings, "pgn_autosave", False):
            return None
        if not self._board.move_stack:
            return None  # nothing worth saving

        pgn_dir = Path(getattr(self._settings, "pgn_dir", "")).expanduser()
        if not str(pgn_dir):
            return None
        try:
            pgn_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log.exception("could not create PGN dir %s", pgn_dir)
            return None

        game = chess.pgn.Game.from_board(self._board)
        white = "Human" if self._human_white else Path(self._engine_path).name
        black = Path(self._engine_path).name if self._human_white else "Human"
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

        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = pgn_dir / f"{ts}-{self._game_id}.pgn"
        try:
            with path.open("w", encoding="utf-8") as f:
                print(game, file=f, end="\n\n")
        except OSError:
            log.exception("could not write PGN to %s", path)
            return None
        log.info("saved PGN to %s", path)
        return path
