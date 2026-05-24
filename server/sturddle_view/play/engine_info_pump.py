"""Shared engine-info pump.

One loop, two callers: HVE's gameplay/analysis path and the AI agent's
`analyze` tool. Both consume a `chess.engine.SimpleAnalysisResult`
async-iterable, both filter to "interesting" info chunks
(those carrying pv / depth / score), both serialize to the unified
schema and publish `engine_info` events to the bus.

Diverging this into two loops bit us once already: the AI tool was
running its own no-publish loop, so the PV-table and arrow stayed
empty even though the engine was producing info. Keep the loop here.
"""
from __future__ import annotations

from typing import Callable, Optional

import chess
import chess.engine

from ..chess.engine_info import serialize_info
from ..events import Event, EventBus
from ..llm.cancel import CancelToken


async def pump_engine_info(
    analysis: chess.engine.SimpleAnalysisResult,
    *,
    bus: EventBus,
    game_id: str,
    board: chess.Board,
    pov: chess.Color,
    on_payload: Optional[Callable[[dict], None]] = None,
    capture_score: Optional[dict] = None,
    cancel_token: Optional[CancelToken] = None,
) -> tuple[chess.engine.InfoDict, bool]:
    """Drain `analysis` until completion or cooperative cancel.

    For each "interesting" info chunk (carries pv / depth / score):
    - serialize to the unified engine_info schema using the caller's POV
    - call `on_payload(payload)` if provided (HVE uses this to cache for
      `/game/sync` replay; the AI tool passes None)
    - if `capture_score` is provided, store the deepest seen white-POV
      score there (caller-owned dict; cleared then populated)
    - publish an `engine_info` event to the bus so the PV-table window
      and the board arrow update

    Cancellation:
    - When `cancel_token` is set and flips during the loop, the helper
      calls `analysis.stop()`, drains remaining info chunks so the
      engine sees bestmove and the context manager exits cleanly, and
      returns with the second tuple element True. Callers that rely on
      asyncio task cancellation (HVE) can leave `cancel_token=None`.

    Returns `(last_info, cancelled)`. `last_info` is the most recent
    raw `InfoDict` seen so callers (e.g. the AI tool building its
    return value) do not have to re-iterate.
    """
    last: chess.engine.InfoDict = {}
    cancelled = False
    async for info in analysis:
        if info:
            last = info
        if "pv" in info or "depth" in info or "score" in info:
            payload = serialize_info(info, board=board, pov=pov)
            if on_payload is not None:
                on_payload(payload)
            if capture_score is not None and "score" in info:
                side = info["score"].pov(chess.WHITE)
                entry: dict
                if side.is_mate():
                    entry = {"mate": side.mate()}
                else:
                    entry = {"cp": side.score()}
                if "depth" in info:
                    entry["depth"] = info["depth"]
                capture_score.clear()
                capture_score.update(entry)
            await bus.publish(
                Event(kind="engine_info", game_id=game_id, payload=payload)
            )
        if cancel_token is not None and cancel_token.cancelled:
            cancelled = True
            try:
                analysis.stop()
            except Exception:
                pass
            async for _ in analysis:
                pass
            break
    return last, cancelled
