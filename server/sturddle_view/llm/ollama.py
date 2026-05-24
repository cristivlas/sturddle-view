"""Ollama (OpenAI-compatible) provider stub. Real body lands first when
we flesh out real providers (token savings during dev).
"""
from __future__ import annotations

from typing import AsyncIterator

from .base import LLMProvider, ProviderChunk


class OllamaProvider(LLMProvider):
    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def stream(self, system: str, user_msg: str) -> AsyncIterator[ProviderChunk]:
        raise NotImplementedError("Ollama provider not yet implemented")
        yield  # pragma: no cover - marks this as an async generator
