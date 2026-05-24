"""Hardcoded-response provider for the walking-skeleton spike and tests.

First-class repo citizen, not throwaway: hardens the abstraction boundary
and lets the agent + transport + UI run end-to-end with no network and
no API key. Tests use this directly; the runtime uses it whenever the
selected provider's real backend isn't wired in yet.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Sequence

from .base import LLMProvider, Message, ProviderChunk, ToolSpec


SKELETON_CHUNKS: tuple[str, ...] = (
    "Walking skeleton: this prose is canned.\n\n",
    "The full pipeline is live -- provider, coordinator, websocket bus, ",
    "panel rendering -- but no real LLM call happens yet.\n",
)


class CannedProvider(LLMProvider):
    def __init__(self, chunks: Sequence[str] = SKELETON_CHUNKS) -> None:
        self._chunks = tuple(chunks)

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        for chunk in self._chunks:
            yield ProviderChunk(kind="text", text=chunk)
            # Yield control between chunks so the event loop can flush
            # each chunk to subscribers before the next one is produced;
            # also keeps cancellation responsive.
            await asyncio.sleep(0)
