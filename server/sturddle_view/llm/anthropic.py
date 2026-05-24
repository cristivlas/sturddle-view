"""Anthropic provider stub. Real streaming + tool-use body lands in a
later Phase 0 cycle; constructor signature is locked now so the rest of
the system can target it.
"""
from __future__ import annotations

from typing import AsyncIterator

from .base import LLMProvider, ProviderChunk


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def stream(self, system: str, user_msg: str) -> AsyncIterator[ProviderChunk]:
        raise NotImplementedError("Anthropic provider not yet implemented")
        yield  # pragma: no cover - marks this as an async generator
