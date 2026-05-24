"""Per-call cancellation token for tool execution.

Each agent-turn dispatch creates a fresh `CancelToken` and passes it
into the tool callable. The runner flips the token on user cancel,
giving the tool a cooperative chance to abort cleanly before the
asyncio task is forcibly cancelled.

Day-1 design for parallel-tool tolerance (spec §Triggers,
Forward-looking): per-call tokens mean concurrent tools don't share
cancellation state.
"""
from __future__ import annotations

import asyncio


class CancelToken:
    def __init__(self) -> None:
        # asyncio.Event is set-once + multi-waiter friendly. Sync
        # `.cancel()` flips it; async `.wait_cancelled()` waits.
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait_cancelled(self) -> None:
        await self._event.wait()
