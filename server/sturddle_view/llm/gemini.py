"""Google Gemini provider (OpenAI-compatible surface).

Gemini exposes an OpenAI-compatible chat-completions endpoint, so this
provider reuses the shared wire layer in `openai_compat` (translation +
SSE loop) just like Ollama. The differences are auth (a bearer API key
rather than a local daemon), the base URL, and thinking via Gemini's
`reasoning_effort` field on the compat surface.

Free-tier note: the free tier has tight per-minute / per-day request
limits. The agent loop makes one HTTP round per tool round plus one per
delegated verifier sub-run, so a single rich analysis can hit those
limits. Rate-limit errors (HTTP 429) surface through the shared error
path as a terminal done/error event -> toast, same as any other provider
failure.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import httpx

from ._errors import extract_error_message
from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec
from .inline_tool_calls import recover_inline_tool_calls
from .openai_compat import (
    inline_recovery_args,
    messages_anthropic_to_openai,
    stream_openai_compat,
    tools_anthropic_to_openai,
)
from .transcript import Transcript


log = logging.getLogger(__name__)


# OpenAI-compatibility base; note the trailing path includes `/openai`.
# The chat + models endpoints hang off this prefix.
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# Per-request timeout for the control-plane endpoint (/models). Streaming
# chat uses no timeout (long generations) like the other providers.
_CONTROL_TIMEOUT_S = 10.0

# Gemini's compat surface maps a thinking budget onto OpenAI's
# `reasoning_effort` enum. We pick "low" when thinking is enabled: it
# turns reasoning on without spending the larger budgets that would
# dominate latency on the free tier. budget_tokens is not honored by the
# compat surface, so the Settings budget input stays Anthropic-only (the
# UI already hides it for non-Anthropic providers).
_REASONING_EFFORT_ON = "low"


class GeminiProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        thinking_enabled: bool = False,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._thinking_enabled = thinking_enabled

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def list_models(self) -> list[str]:
        """List available Gemini models via the compat `/models` endpoint.

        Requires the user's API key. Raises RuntimeError on auth / network
        failure so the API layer surfaces a useful error to the UI (which
        then falls back to free-text model entry).
        """
        if not self._api_key:
            raise RuntimeError("gemini: API key not configured")
        url = f"{self._base_url}/models"
        async with httpx.AsyncClient(timeout=_CONTROL_TIMEOUT_S) as client:
            resp = await client.get(url, headers=self._auth_headers())
            if resp.status_code != 200:
                raise RuntimeError(
                    f"gemini /models returned {resp.status_code}: "
                    f"{extract_error_message(resp.text)}"
                )
            body = resp.json()
        data = body.get("data") or []
        ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
        # Compat ids come back prefixed ("models/gemini-2.5-flash"); strip
        # the prefix so the dropdown shows bare model names that the chat
        # endpoint also accepts.
        ids = [i.split("/", 1)[1] if i.startswith("models/") else i for i in ids]
        return sorted(set(ids))

    def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None = None,
        *,
        transcript: Transcript | None = None,
        round_index: int = 0,
        thinking: bool | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        if not self._api_key:
            raise RuntimeError("gemini: API key not configured")

        wire_messages: list[dict] = []
        if system:
            wire_messages.append({"role": "system", "content": system})
        wire_messages.extend(messages_anthropic_to_openai(messages))

        body: dict[str, Any] = {
            "model": self._model,
            "messages": wire_messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools_anthropic_to_openai(tools)
        # `thinking=False` forces it off for this call (verifier sub-runs);
        # otherwise honor the provider default.
        if thinking is not False and self._thinking_enabled:
            body["reasoning_effort"] = _REASONING_EFFORT_ON

        inner = stream_openai_compat(
            self,
            url=f"{self._base_url}/chat/completions",
            body=body,
            headers=self._auth_headers(),
            error_label="gemini",
            transcript=transcript,
            round_index=round_index,
        )
        # Reuse the same inline-tool-call recovery as Ollama: free-tier
        # flash models occasionally emit tool calls as prose. Inert when
        # the model behaves.
        tool_names, tool_schemas = inline_recovery_args(tools)
        return recover_inline_tool_calls(
            inner, tool_names=tool_names, tool_schemas=tool_schemas,
        )
