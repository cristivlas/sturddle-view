"""Ollama (OpenAI-compatible) provider stub. Real body lands first when
we flesh out real providers (token savings during dev).

Wire-format translation responsibility (canonical = Anthropic, spec
§Providers): this provider receives `ToolWireSpec` items in Anthropic
shape (`{name, description, input_schema}`) and must translate to
OpenAI's function-call shape
(`{"type": "function", "function": {name, description, parameters}}`)
before sending. Mirror translation applies on the way back: Ollama
emits OpenAI-shaped tool_calls + tool_role responses, which must be
re-shaped into Anthropic-style `tool_use` ProviderChunks and
`tool_result` blocks before the runner sees them.
"""
from __future__ import annotations

from typing import AsyncIterator

from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec


class OllamaProvider(LLMProvider):
    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        raise NotImplementedError("Ollama provider not yet implemented")
        yield  # pragma: no cover - marks this as an async generator
