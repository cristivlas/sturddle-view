"""Anthropic provider stub.

Constructor signature is locked so callers (provider factory, settings
plumbing) can target it. `list_models()` works against the real API so
the Settings UI can populate a model dropdown ahead of the streaming
body landing; `stream()` still raises NotImplementedError.
"""
from __future__ import annotations

from typing import AsyncIterator

import httpx

from ._errors import extract_error_message
from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec
from .transcript import Transcript


_ANTHROPIC_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    async def list_models(self) -> list[str]:
        """List available Anthropic models via GET /v1/models.

        Requires the user's API key. Raises RuntimeError on
        auth / network failure so the API layer can surface a useful
        error to the UI.
        """
        if not self._api_key:
            raise RuntimeError("anthropic: API key not configured")
        url = f"{_ANTHROPIC_BASE}/v1/models"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"anthropic /v1/models returned {resp.status_code}: "
                    f"{extract_error_message(resp.text[:500])}"
                )
            body = resp.json()
        data = body.get("data") or []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        return sorted(set(ids))

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
