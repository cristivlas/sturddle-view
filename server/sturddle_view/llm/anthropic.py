"""Anthropic provider stub.

Constructor signature is locked so callers (provider factory, settings
plumbing) can target it; the real streaming + tool-use body raises
NotImplementedError until wired up.
"""
from __future__ import annotations

from typing import AsyncIterator

from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec
from .transcript import Transcript


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None = None,
        *,
        transcript: Transcript | None = None,
        round_index: int = 0,
    ) -> AsyncIterator[ProviderChunk]:
        raise NotImplementedError("Anthropic provider not yet implemented")
        yield  # pragma: no cover - marks this as an async generator
