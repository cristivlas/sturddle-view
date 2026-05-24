"""Hardcoded-response provider.

First-class repo citizen, not throwaway: hardens the abstraction boundary
and lets the agent + transport + UI run end-to-end with no network and
no API key. Tests use this directly; the runtime uses it whenever the
selected provider's real backend isn't wired in yet.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Sequence

from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec
from .transcript import Transcript


SKELETON_CHUNKS: tuple[str, ...] = (
    "Canned provider response: the LLM backend is not wired in for ",
    "this turn.\n\n",
    "Provider, coordinator, websocket bus, and panel rendering are all ",
    "live; only the model call is stubbed.\n",
)


class CannedProvider(LLMProvider):
    def __init__(self, chunks: Sequence[str] = SKELETON_CHUNKS) -> None:
        self._chunks = tuple(chunks)

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None = None,
        *,
        transcript: Transcript | None = None,
        round_index: int = 0,
    ) -> AsyncIterator[ProviderChunk]:
        for chunk in self._chunks:
            yield ProviderChunk(kind="text", text=chunk)
            # Yield control between chunks so the event loop can flush
            # each chunk to subscribers before the next one is produced;
            # also keeps cancellation responsive.
            await asyncio.sleep(0)
