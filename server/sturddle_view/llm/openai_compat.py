"""Shared OpenAI-compatible chat-completions wire layer.

Canonical message/tool shape across the codebase is Anthropic's (spec
Providers). Any provider speaking OpenAI's `/v1/chat/completions` API --
Ollama's compat surface, Google Gemini's compat surface -- translates
to/from that shape here so the translation lives in exactly one place.

This module owns:
- the pure translation helpers (Anthropic <-> OpenAI for tools,
  messages, and streamed tool_calls),
- the SSE streaming loop (`stream_openai_compat`) that POSTs a request
  body and re-emits OpenAI deltas as Anthropic-style `ProviderChunk`s.

It does NOT own auth headers, base URLs, or the request-body assembly
that differs per provider -- callers build the body and pass headers in.
Harmony-marker scrubbing is applied to visible + reasoning text so local
models that leak channel tokens are handled uniformly; it is inert for
well-behaved cloud models.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ._errors import extract_error_message
from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec
from .harmony_strip import flush_harmony_carry, strip_harmony_text
from .transcript import Transcript


# Neutral key under which an opaque tool-call signature rides on the
# canonical (Anthropic-shaped) tool_use block. The wire mapping to/from
# Gemini's `extra_content.google.thought_signature` lives in this module;
# the canonical shape stays provider-agnostic.
TOOL_SIGNATURE_KEY = "tool_signature"
# Gemini's OpenAI-compat location for the same value.
_GEMINI_SIG_PATH = ("extra_content", "google", "thought_signature")


def _read_gemini_signature(tool_call: dict) -> str:
    node: object = tool_call
    for key in _GEMINI_SIG_PATH:
        if not isinstance(node, dict):
            return ""
        node = node.get(key)
    return node if isinstance(node, str) else ""


def _gemini_signature_block(signature: str) -> dict:
    extra, google, leaf = _GEMINI_SIG_PATH
    return {extra: {google: {leaf: signature}}}


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
                    tc: dict = {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                    # Echo back an opaque tool signature (Gemini requires
                    # its thought_signature on the first tool_call of each
                    # step or the follow-up request 400s).
                    sig = block.get(TOOL_SIGNATURE_KEY)
                    if sig:
                        tc.update(_gemini_signature_block(sig))
                    tool_calls.append(tc)
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


class MalformedToolArgumentsError(RuntimeError):
    """The model's accumulated tool_call.arguments is not valid JSON.

    Carries the raw string so callers / transcripts can surface what the
    model actually produced. This is the seam where a future JSON-repair
    pass would hook in.
    """
    def __init__(self, tool_name: str, raw_arguments: str, parse_error: str) -> None:
        super().__init__(
            f"openai-compat: tool {tool_name!r} arguments are not valid JSON "
            f"({parse_error}); raw={raw_arguments!r}"
        )
        self.tool_name = tool_name
        self.raw_arguments = raw_arguments
        self.parse_error = parse_error


def openai_tool_call_to_provider_chunk(tool_call: dict) -> ProviderChunk:
    """Accumulated OpenAI tool_call (from streamed deltas) -> Anthropic
    tool_use ProviderChunk.

    Raises `MalformedToolArgumentsError` on bad JSON rather than silently
    coercing to `{}` -- a model that emits broken JSON is a real problem,
    and silently passing `{}` to the tool just hides it. The transcript
    will have already captured the raw byte trail.
    """
    fn = tool_call.get("function", {}) or {}
    tool_name = fn.get("name", "") or ""
    args_raw = fn.get("arguments", "")
    if args_raw:
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError as exc:
            raise MalformedToolArgumentsError(
                tool_name=tool_name, raw_arguments=args_raw, parse_error=str(exc),
            ) from exc
    else:
        args = {}
    return ProviderChunk(
        kind="tool_use",
        tool_use_id=tool_call.get("id", "") or "",
        tool_name=tool_name,
        tool_input=args,
        tool_signature=_read_gemini_signature(tool_call),
    )


# ----- Streaming loop -----


async def stream_openai_compat(
    provider: LLMProvider,
    *,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    error_label: str,
    transcript: Transcript | None,
    round_index: int,
) -> AsyncIterator[ProviderChunk]:
    """Run one OpenAI-compatible chat-completions SSE round.

    The caller assembles `body` (model, messages, tools, stream=True, plus
    any provider-specific fields) and `headers` (auth). This loop captures
    the request to the transcript, streams the SSE response, scrubs
    harmony markers from visible + reasoning text, and emits accumulated
    tool_calls after the text -- matching the "text first, tool_use last"
    ordering the coordinator's round reassembly expects.

    `error_label` prefixes the RuntimeError on a non-200 (e.g. "ollama",
    "gemini") so toasts name the failing provider. `provider` is only used
    for its transcript helpers (`_tx_request` / `_tx_wire`).
    """
    await provider._tx_request(transcript, round_index, body)

    # Per-line accumulator for tool_call deltas: id+name arrive once near
    # the start, arguments stream as a concatenated string. Indexed by
    # `index` field in the OpenAI delta protocol.
    tool_call_buf: dict[int, dict] = {}
    # Harmony marker carry-buffers. See harmony_strip.py -- some models
    # leak `<|...|>` tokens into both visible content AND the reasoning
    # channel; each needs its own carry because deltas interleave.
    text_carry: list[str] = []
    reason_carry: list[str] = []

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            url,
            json=body,
            headers=headers,
        ) as resp:
            if resp.status_code != 200:
                raw = (await resp.aread()).decode("utf-8", errors="replace")
                await provider._tx_wire(
                    transcript, round_index, f"HTTP {resp.status_code}: {raw}"
                )
                raise RuntimeError(
                    f"{error_label} API error {resp.status_code}: "
                    f"{extract_error_message(raw)}"
                )
            async for line in resp.aiter_lines():
                if not line:
                    continue
                # Capture every raw line BEFORE parsing so a crash
                # downstream still leaves the byte trail behind.
                await provider._tx_wire(transcript, round_index, line)
                # OpenAI SSE: each chunk is "data: {...}". A terminal
                # "data: [DONE]" marks end of stream.
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"{error_label}: malformed SSE payload: {payload!r} ({exc})"
                    ) from exc
                choices = evt.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    scrubbed = strip_harmony_text(content, text_carry)
                    if scrubbed:
                        yield ProviderChunk(kind="text", text=scrubbed)
                # Some models stream chain-of-thought into `reasoning` and
                # never fill `content`. Surface as thinking so it lands in
                # the transcript labeled; the coordinator drops these for
                # the UI but counts the round.
                reasoning = delta.get("reasoning")
                if reasoning:
                    scrubbed = strip_harmony_text(reasoning, reason_carry)
                    if scrubbed:
                        yield ProviderChunk(kind="thinking", text=scrubbed)
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
                    # Gemini rides the thought_signature on the tool_call's
                    # extra_content; keep the first non-empty one we see for
                    # this index (it arrives once, near the start).
                    sig = _read_gemini_signature(tcd)
                    if sig and not _read_gemini_signature(slot):
                        slot.update(_gemini_signature_block(sig))

    # Surface any held-back partial that never completed into a marker, so
    # user text containing literal `<` is not silently dropped at stream
    # end.
    tail = flush_harmony_carry(text_carry)
    if tail:
        yield ProviderChunk(kind="text", text=tail)
    reason_tail = flush_harmony_carry(reason_carry)
    if reason_tail:
        yield ProviderChunk(kind="thinking", text=reason_tail)

    # Emit accumulated tool_calls (if any) AFTER text streaming completes
    # -- matches Anthropic's "text first, tool_use last" ordering that the
    # coordinator's round_chunks reassembly expects.
    for idx in sorted(tool_call_buf.keys()):
        yield openai_tool_call_to_provider_chunk(tool_call_buf[idx])


def ordered_param_names(input_schema: dict) -> list[str]:
    """Extract param names from a JSON Schema `input_schema`, ordered by
    `required` first (in declared order) then any remaining `properties`
    keys (dict insertion order). Drives positional-arg recovery in
    inline-recovery's BareJsonFlavor."""
    props = input_schema.get("properties") or {}
    if not isinstance(props, dict):
        return []
    required = input_schema.get("required") or []
    if not isinstance(required, list):
        required = []
    ordered: list[str] = []
    seen: set[str] = set()
    for name in required:
        if isinstance(name, str) and name in props and name not in seen:
            ordered.append(name)
            seen.add(name)
    for name in props:
        if name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


def inline_recovery_args(
    tools: list[ToolWireSpec] | None,
) -> tuple[set[str], dict[str, list[str]]]:
    """Build the (tool_names, tool_schemas) pair that
    `recover_inline_tool_calls` needs from a wire tool list.

    Some models stream tool calls as prose; the recovery wrapper needs
    the tool-name set (call-syntax shape) and ordered param names per
    tool (positional bare-JSON shape).
    """
    tool_names: set[str] = set()
    tool_schemas: dict[str, list[str]] = {}
    for t in tools or []:
        n = t.get("name")
        if not n:
            continue
        tool_names.add(n)
        params = ordered_param_names(t.get("input_schema") or {})
        if params:
            tool_schemas[n] = params
    return tool_names, tool_schemas
