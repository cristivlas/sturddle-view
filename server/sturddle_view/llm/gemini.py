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
# The chat endpoint hangs off this prefix. Model listing uses the native
# base below (the compat /openai/models omits capability metadata).
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"

# Suffix that turns the OpenAI-compat base into the native one. The native
# /v1beta/models endpoint returns `supportedGenerationMethods` per model;
# the compat /openai/models does not, so capability filtering needs this.
_OPENAI_SUFFIX = "/openai"

# Native generation method a chat model advertises. Filtering on it drops
# embedding/predict-only SKUs. (Image/tts variants also advertise it but
# still 400 on the chat surface -- see list_models for why they're kept.)
_CHAT_GENERATION_METHOD = "generateContent"

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
        # Compat surface (chat/completions) takes OpenAI-style Bearer auth.
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _native_headers(self) -> dict[str, str]:
        # Native surface (/v1beta/models) rejects Bearer with 401; it wants
        # the key in x-goog-api-key.
        return {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }

    def _native_base(self) -> str:
        """Native API base (drops the `/openai` compat suffix). Model
        listing uses it because the compat surface omits the capability
        metadata needed to filter chat-usable models."""
        if self._base_url.endswith(_OPENAI_SUFFIX):
            return self._base_url[: -len(_OPENAI_SUFFIX)]
        return self._base_url

    async def list_models(self) -> list[str]:
        """Native /models, kept to those advertising `generateContent`.

        Drops embedding/predict SKUs; image/tts variants stay (they also
        advertise it but 400 on chat -- no metadata distinguishes them, and
        a name blocklist risks hiding valid models). Raises on auth/network
        failure so the UI falls back to free-text entry.
        """
        if not self._api_key:
            raise RuntimeError("gemini: API key not configured")
        url = f"{self._native_base()}/models"
        async with httpx.AsyncClient(timeout=_CONTROL_TIMEOUT_S) as client:
            resp = await client.get(url, headers=self._native_headers())
            if resp.status_code != 200:
                raise RuntimeError(
                    f"gemini /models returned {resp.status_code}: "
                    f"{extract_error_message(resp.text)}"
                )
            body = resp.json()
        models = body.get("models") or []
        ids: list[str] = []
        for m in models:
            if not isinstance(m, dict):
                continue
            name = m.get("name")
            methods = m.get("supportedGenerationMethods") or []
            if not name or _CHAT_GENERATION_METHOD not in methods:
                continue
            # Native names are prefixed ("models/gemini-2.5-flash"); strip
            # so the dropdown shows bare ids the chat endpoint also accepts.
            ids.append(name.split("/", 1)[1] if name.startswith("models/") else name)
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
