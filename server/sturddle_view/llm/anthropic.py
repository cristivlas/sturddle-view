"""Anthropic provider.

Canonical wire shape (Anthropic native): tools/messages pass through
unmodified. The provider POSTs `/v1/messages` with `stream=true` and
re-emits each SSE delta as a ProviderChunk.

SSE shape we care about:
- `content_block_start` -- opens a block. For `tool_use` blocks the id
  and name arrive here; for `text` and `thinking` blocks, just the type.
- `content_block_delta` -- delta payload:
    * `text_delta` for text/thinking,
    * `input_json_delta.partial_json` for tool_use args (streamed JSON).
- `content_block_stop` -- closes the block. Tool_use is emitted now
  (after we have the full input JSON).
- `message_delta` / `message_stop` -- terminal; we just stop reading.

Errors mid-stream arrive as `event: error` with a JSON envelope. HTTP
errors before the stream opens come back as a non-200 status.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

import httpx

from ._errors import extract_error_message
from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec
from .transcript import Transcript


log = logging.getLogger(__name__)


_ANTHROPIC_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
# Spec doesn't constrain max_tokens; pick a generous cap so the model
# rarely truncates a coaching prose response. Env override for ops.
_DEFAULT_MAX_TOKENS = 4096
# Anthropic API requires budget_tokens < max_tokens. When thinking is
# enabled we add the budget on top of the visible-output cap.
_ADAPTIVE_THINKING_MIN_MAJOR = 4
_ADAPTIVE_THINKING_MIN_MINOR = 6


class _ToolUseAccumulator:
    """Builds one tool_use ProviderChunk from streamed input_json_delta
    fragments. Anthropic streams the args as a JSON string in pieces; we
    concatenate, then parse at content_block_stop. Bad JSON raises so the
    coordinator surfaces it -- silently coercing to {} would hide the
    real problem."""

    def __init__(self, tool_use_id: str, tool_name: str) -> None:
        self.tool_use_id = tool_use_id
        self.tool_name = tool_name
        self._partial: list[str] = []

    def append_partial_json(self, frag: str) -> None:
        self._partial.append(frag)

    def to_chunk(self) -> ProviderChunk:
        raw = "".join(self._partial)
        if raw:
            try:
                args = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"anthropic: tool {self.tool_name!r} arguments not valid "
                    f"JSON ({exc}); raw={raw!r}"
                ) from exc
        else:
            args = {}
        return ProviderChunk(
            kind="tool_use",
            tool_use_id=self.tool_use_id,
            tool_name=self.tool_name,
            tool_input=args,
        )


class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        thinking_enabled: bool = False,
        thinking_budget_tokens: int = 0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._thinking_enabled = thinking_enabled
        self._thinking_budget_tokens = thinking_budget_tokens

    def _use_adaptive_thinking(self) -> bool:
        # claude-opus-4-6+ requires {"type": "adaptive"}; older Opus and
        # Sonnet 3.7+ use {"type": "enabled", "budget_tokens": N}.
        m = re.match(r"claude-opus-(\d+)-(\d+)", self._model or "")
        if m is None:
            return False
        major, minor = int(m.group(1)), int(m.group(2))
        return (major, minor) >= (_ADAPTIVE_THINKING_MIN_MAJOR, _ADAPTIVE_THINKING_MIN_MINOR)

    def _thinking_param(self) -> dict:
        if self._use_adaptive_thinking():
            return {"type": "adaptive"}
        return {"type": "enabled", "budget_tokens": self._thinking_budget_tokens}

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
        thinking: bool | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        if not self._api_key:
            raise RuntimeError("anthropic: API key not configured")

        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": messages,
            "stream": True,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
        # `thinking=False` forces it off for this call (verifier sub-runs).
        if thinking is not False and self._thinking_enabled:
            body["thinking"] = self._thinking_param()
            # Anthropic requires max_tokens > budget_tokens; lift the cap
            # so visible output isn't squeezed by reasoning.
            if "budget_tokens" in body["thinking"]:
                body["max_tokens"] = _DEFAULT_MAX_TOKENS + self._thinking_budget_tokens

        log.info(
            "anthropic stream: model=%s thinking=%s",
            self._model,
            body.get("thinking"),
        )
        await self._tx_request(transcript, round_index, body)

        url = f"{_ANTHROPIC_BASE}/v1/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

        # Index -> open block state. For text/thinking we just remember
        # the kind so deltas route correctly; for tool_use we keep a full
        # accumulator and emit at content_block_stop.
        open_text_kinds: dict[int, str] = {}
        open_tools: dict[int, _ToolUseAccumulator] = {}

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", url, json=body, headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    await self._tx_wire(transcript, round_index, f"HTTP {resp.status_code}: {raw}")
                    raise RuntimeError(
                        f"anthropic API error {resp.status_code}: {extract_error_message(raw)}"
                    )
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    await self._tx_wire(transcript, round_index, line)
                    # Anthropic SSE: alternating `event: <name>` and
                    # `data: <json>`. We only act on data lines; the event
                    # type is also carried inside the JSON payload as
                    # `type`, so we route off that and ignore the event:
                    # marker.
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload:
                        continue
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"anthropic: malformed SSE payload: {payload!r} ({exc})"
                        ) from exc
                    etype = evt.get("type")
                    if etype == "content_block_start":
                        idx = evt.get("index", 0)
                        block = evt.get("content_block") or {}
                        btype = block.get("type")
                        if btype == "tool_use":
                            open_tools[idx] = _ToolUseAccumulator(
                                tool_use_id=block.get("id", "") or "",
                                tool_name=block.get("name", "") or "",
                            )
                        elif btype in ("text", "thinking"):
                            open_text_kinds[idx] = btype
                    elif etype == "content_block_delta":
                        idx = evt.get("index", 0)
                        delta = evt.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            kind = open_text_kinds.get(idx, "text")
                            text = delta.get("text") or ""
                            if text:
                                yield ProviderChunk(kind=kind, text=text)
                        elif dtype == "thinking_delta":
                            # Anthropic's extended thinking surfaces here
                            # (separate from text_delta on thinking blocks).
                            text = delta.get("thinking") or ""
                            if text:
                                yield ProviderChunk(kind="thinking", text=text)
                        elif dtype == "input_json_delta":
                            acc = open_tools.get(idx)
                            if acc is not None:
                                acc.append_partial_json(delta.get("partial_json", "") or "")
                    elif etype == "content_block_stop":
                        idx = evt.get("index", 0)
                        acc = open_tools.pop(idx, None)
                        if acc is not None:
                            yield acc.to_chunk()
                        open_text_kinds.pop(idx, None)
                    elif etype == "error":
                        err = evt.get("error") or {}
                        msg = err.get("message") or json.dumps(err)
                        raise RuntimeError(f"anthropic stream error: {msg}")
                    # message_start / message_delta / message_stop / ping
                    # carry no chunk-relevant data for us.
