"""Human vs engine driver, on top of python-chess's async UCI interface.

Skips the tournament manager and proxy entirely. Streams engine info (depth,
score, PV, NPS) onto the same event bus the tournament path uses.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

import chess
import chess.engine

from ..events import Event, EventBus

log = logging.getLogger(__name__)


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
    return out


class HumanVsEngine:
    """Single-game driver. Holds one active game at a time."""

    def __init__(self, engine_path: str, bus: EventBus) -> None:
        self._engine_path = engine_path
        self._bus = bus
        self._engine: chess.engine.UciProtocol | None = None
        self._board: chess.Board | None = None
        self._game_id: str | None = None
        self._human_white: bool = True
        self._tc: TimeControl = TimeControl(300.0, 0.0)
        self._white_time: float = 0.0
        self._black_time: float = 0.0
        self._think_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _ensure_engine(self) -> chess.engine.UciProtocol:
        if self._engine is None:
            _transport, engine = await chess.engine.popen_uci(self._engine_path)
            self._engine = engine
        return self._engine

    async def new_game(self, human_white: bool, tc: TimeControl) -> str:
        async with self._lock:
            await self._cancel_think()
            engine = await self._ensure_engine()
            engine.send_line("ucinewgame")
            self._board = chess.Board()
            self._human_white = human_white
            self._tc = tc
            self._white_time = tc.initial_seconds
            self._black_time = tc.initial_seconds
            self._game_id = uuid.uuid4().hex[:12]
            await self._publish_board()
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
            self._board.push(move)
            await self._publish_board()
            ended = self._board.is_game_over()
        if ended:
            await self._publish_result()
        else:
            await self._engine_to_move()

    async def resign(self) -> None:
        async with self._lock:
            await self._cancel_think()
            if self._game_id is None:
                return
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
            if self._engine is not None:
                try:
                    await self._engine.quit()
                except (chess.engine.EngineTerminatedError, RuntimeError, BrokenPipeError):
                    pass
                self._engine = None

    async def _cancel_think(self) -> None:
        if self._think_task and not self._think_task.done():
            self._think_task.cancel()
            try:
                await self._think_task
            except (asyncio.CancelledError, Exception):
                pass
        self._think_task = None

    async def _engine_to_move(self) -> None:
        # Spawn the search in a background task so the HTTP response returns
        # immediately. The task drives the bus directly.
        self._think_task = asyncio.create_task(self._think_and_play())

    async def _think_and_play(self) -> None:
        assert self._engine is not None and self._board is not None and self._game_id is not None
        game_id = self._game_id
        board = self._board
        engine = self._engine
        limit = chess.engine.Limit(
            white_clock=self._white_time,
            black_clock=self._black_time,
            white_inc=self._tc.increment_seconds,
            black_inc=self._tc.increment_seconds,
        )
        try:
            with await engine.analysis(board, limit=limit) as analysis:
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
                best = (await result).move
        except chess.engine.EngineTerminatedError:
            log.exception("engine terminated mid-search")
            await self._bus.publish(
                Event(kind="system", game_id=game_id, payload={"error": "engine_terminated"})
            )
            return
        except asyncio.CancelledError:
            return
        if best is None:
            return
        async with self._lock:
            if self._board is None or self._game_id != game_id:
                return
            self._board.push(best)
            await self._publish_board()
            ended = self._board.is_game_over()
        if ended:
            await self._publish_result()

    async def _publish_board(self) -> None:
        assert self._board is not None and self._game_id is not None
        await self._bus.publish(
            Event(
                kind="board_update",
                game_id=self._game_id,
                payload={
                    "fen": self._board.fen(),
                    "turn": "white" if self._board.turn else "black",
                    "ply": self._board.ply(),
                    "last_move": self._board.peek().uci() if self._board.move_stack else None,
                },
            )
        )

    async def _publish_result(self) -> None:
        assert self._board is not None and self._game_id is not None
        outcome = self._board.outcome()
        payload = {
            "result": outcome.result() if outcome else "*",
            "termination": outcome.termination.name.lower() if outcome else "unknown",
        }
        await self._bus.publish(
            Event(kind="game_result", game_id=self._game_id, payload=payload)
        )
