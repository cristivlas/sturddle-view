"""Typed event bus.

All live activity (engine info, board updates, clock ticks, tournament
standings, AI analysis stream) flows through this bus. Clients consume
it via WebSocket.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


# Stable for the lifetime of this process. Emitted on every outgoing
# event so the client can detect a server restart and reload, dropping
# stale UI state (view-mode cursor, dismissed toasts, edit drafts) that
# the new server session can't possibly honor.
SESSION_EPOCH = uuid.uuid4().hex


EventKind = Literal[
    "engine_info",
    "uci_log",
    "board_update",
    "clock_tick",
    "clock_update",
    "game_result",
    "tournament_update",
    "tournament_status",
    "sprt_update",
    "ai_info",
    "ai_thinking",
    "ai_tool_call",
    "ai_tool_call_failed",
    "ai_corrective",
    "ai_recommendation",
    "system",
]


@dataclass(slots=True)
class Event:
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    game_id: str | None = None


class EventBus:
    """In-process pub/sub. Each subscriber gets its own bounded queue."""

    def __init__(self, queue_size: int = 1024) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_size = queue_size
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    async def publish(self, event: Event) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop oldest, push new. Observability gap is acceptable;
                # blocking the publisher is not.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass
