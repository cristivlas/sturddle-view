"""AI analysis coordinator.

Owns the LLM session for the live play path (path 1 in the spec). Drains
the provider's chunk stream onto the websocket event bus as `ai_info`
events. Path 2/3 (view, post-game) layer onto this in later phases.

Walking-skeleton scope: no system prompt, no tools, no token caps, no
rolling session, no cancel plumbing -- those land as separate cycles.
The contract this class exposes (`run`, `cancel`) is the target shape.
"""
from __future__ import annotations

import asyncio
import logging

from ..events import Event, EventBus
from ..llm import LLMProvider


log = logging.getLogger(__name__)


# Concurrency: spec says "1 analysis at a time". A single lock owned by
# the coordinator instance is enough -- the coordinator itself is a
# singleton on the app state (composed by play perspective).
class AIAnalysisCoordinator:
    def __init__(self, bus: EventBus, provider: LLMProvider) -> None:
        self._bus = bus
        # Default provider for callers that don't supply one per turn.
        # Per-turn override (run(provider=...)) is the seam later phases
        # use to swap prompts / agents without rebuilding the coordinator.
        self._provider = provider
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def run(
        self,
        *,
        game_id: str | None = None,
        provider: LLMProvider | None = None,
    ) -> None:
        """Run one analysis turn end-to-end. Streams provider chunks as
        `ai_info` events. Blocks until the provider is drained or the
        task is cancelled. Concurrent calls serialize on the lock."""
        active = provider or self._provider
        async with self._lock:
            self._task = asyncio.current_task()
            try:
                async for chunk in active.stream(system="", user_msg=""):
                    if chunk.kind != "text" or not chunk.text:
                        continue
                    await self._bus.publish(
                        Event(
                            kind="ai_info",
                            game_id=game_id,
                            payload={"delta": chunk.text},
                        )
                    )
                await self._bus.publish(
                    Event(
                        kind="ai_info",
                        game_id=game_id,
                        payload={"done": True},
                    )
                )
            except asyncio.CancelledError:
                await self._bus.publish(
                    Event(
                        kind="ai_info",
                        game_id=game_id,
                        payload={"done": True, "cancelled": True},
                    )
                )
                raise
            finally:
                self._task = None

    async def cancel(self) -> None:
        """Cancel the running turn (if any). Hard-stop per spec: drops the
        agent loop, surfaces partial prose as-is. Idempotent."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.shield(task)
        except (asyncio.CancelledError, Exception):
            pass
