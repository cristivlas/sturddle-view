"""Ollama (OpenAI-compatible) provider.

Wire-format translation responsibility (canonical = Anthropic, spec
§Providers): this provider receives messages + tools in Anthropic shape
and translates to OpenAI's chat-completions shape on the wire. Mirror
translation applies on the way back: OpenAI's streamed deltas and
tool_calls are re-shaped into Anthropic-style `tool_use` ProviderChunks
before the runner sees them.

Endpoint: `{base_url}/v1/chat/completions` with `stream=true`. Ollama's
OpenAI-compatibility surface is good enough for chat + tool calling;
we do NOT use its native /api/chat here because the canonical shape
needs to stay one format.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec


log = logging.getLogger(__name__)


# ----- Translation helpers (pure functions; covered by unit tests) -----


def tools_anthropic_to_openai(tools: list[ToolWireSpec]) -> list[dict]:
    """Anthropic {name, description, input_schema} ->
    OpenAI {type, function: {name, description, parameters}}."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


def messages_anthropic_to_openai(messages: list[Message]) -> list[dict]:
    """Re-shape Anthropic-style messages into OpenAI's chat shape.

    - user/assistant strings pass through.
    - assistant content lists collapse text blocks into the `content`
      string and tool_use blocks into `tool_calls`.
    - user content lists carrying tool_results split into one
      `role: tool` message per result (OpenAI requires that).
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "assistant" and isinstance(content, list):
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
            new_msg: dict = {
                "role": "assistant",
                "content": "".join(text_parts),
            }
            if tool_calls:
                new_msg["tool_calls"] = tool_calls
            out.append(new_msg)
            continue
        if role == "user" and isinstance(content, list):
            # Collect tool_results into separate `tool` messages; mix
            # of tool_result + free text is unusual but we leave any
            # non-tool_result blocks in a residual user message.
            residual_text: list[str] = []
            for block in content:
                btype = block.get("type")
                if btype == "tool_result":
                    out.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": block.get("content", ""),
                    })
                elif btype == "text":
                    residual_text.append(block.get("text", ""))
            if residual_text:
                out.append({"role": "user", "content": "".join(residual_text)})
            continue
        # Plain string content (or anything we don't transform) passes through.
        out.append({"role": role, "content": content})
    return out


def openai_tool_call_to_provider_chunk(tool_call: dict) -> ProviderChunk:
    """Accumulated OpenAI tool_call (from streamed deltas) -> Anthropic
    tool_use ProviderChunk. Caller is responsible for accumulating the
    streamed delta fragments before invoking this."""
    fn = tool_call.get("function", {}) or {}
    args_raw = fn.get("arguments", "")
    if args_raw:
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            args = {}
    else:
        args = {}
    return ProviderChunk(
        kind="tool_use",
        tool_use_id=tool_call.get("id", "") or "",
        tool_name=fn.get("name", "") or "",
        tool_input=args,
    )


# ----- Provider -----


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
        # Assemble OpenAI-shaped request. System prompt is a separate
        # first message in OpenAI's API; coordinator passes it as a
        # bare string so we wrap it here.
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

        url = f"{self._base_url}/v1/chat/completions"
        # Per-line accumulator for tool_call deltas: id+name arrive once
        # near the start, arguments stream as a concatenated string.
        # Indexed by `index` field in the OpenAI delta protocol.
        tool_call_buf: dict[int, dict] = {}

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    raise RuntimeError(
                        f"ollama API error {resp.status_code}: {detail}"
                    )
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    # OpenAI SSE: each chunk is "data: {...}". A
                    # terminal "data: [DONE]" marks end of stream.
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        log.warning("ollama: malformed SSE payload, skipping")
                        continue
                    choices = evt.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield ProviderChunk(kind="text", text=content)
                    tc_deltas = delta.get("tool_calls") or []
                    for tcd in tc_deltas:
                        idx = tcd.get("index", 0)
                        slot = tool_call_buf.setdefault(idx, {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if tcd.get("id"):
                            slot["id"] = tcd["id"]
                        fn = tcd.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]

        # Emit accumulated tool_calls (if any) AFTER text streaming
        # completes -- matches Anthropic's "text first, tool_use last"
        # ordering that the coordinator's round_chunks reassembly
        # expects.
        for idx in sorted(tool_call_buf.keys()):
            yield openai_tool_call_to_provider_chunk(tool_call_buf[idx])
