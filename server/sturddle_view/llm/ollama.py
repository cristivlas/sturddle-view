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

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

from ._errors import extract_error_message
from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec
from .harmony_strip import flush_harmony_carry, strip_harmony_text
from .inline_tool_calls import recover_inline_tool_calls
from .transcript import Transcript


log = logging.getLogger(__name__)


DEFAULT_BASE_URL = "http://localhost:11434"

# Max parallel /api/show calls during list_models capability filtering.
# Bounds simultaneous load on the local daemon when a user has many
# models pulled. Cheap calls (single-digit ms each); the cap mostly
# keeps things polite.
_LIST_MODELS_SHOW_CONCURRENCY = 8

# Per-request timeout for the daemon's control-plane endpoints
# (/api/generate keep_alive, /api/ps, /v1/models, /api/show). Streaming
# chat has its own (much longer) timeout elsewhere in this module.
_CONTROL_TIMEOUT_S = 10.0

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


class MalformedToolArgumentsError(RuntimeError):
    """The model's accumulated tool_call.arguments is not valid JSON.

    Carries the raw string so callers / transcripts can surface what the
    model actually produced. This is the seam where a future JSON-repair
    pass would hook in.
    """
    def __init__(self, tool_name: str, raw_arguments: str, parse_error: str) -> None:
        super().__init__(
            f"ollama: tool {tool_name!r} arguments are not valid JSON "
            f"({parse_error}); raw={raw_arguments!r}"
        )
        self.tool_name = tool_name
        self.raw_arguments = raw_arguments
        self.parse_error = parse_error


def messages_anthropic_to_ollama_native(messages: list[Message]) -> list[dict]:
    """Like messages_anthropic_to_openai but for Ollama's native /api/chat:
    tool messages carry no tool_call_id (Ollama matches by order in the
    conversation, not by id); assistant tool_calls use dict arguments and
    drop the id field."""
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
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": block.get("input", {}),
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
            residual_text: list[str] = []
            for block in content:
                btype = block.get("type")
                if btype == "tool_result":
                    out.append({
                        "role": "tool",
                        "content": block.get("content", ""),
                    })
                elif btype == "text":
                    residual_text.append(block.get("text", ""))
            if residual_text:
                out.append({"role": "user", "content": "".join(residual_text)})
            continue
        out.append({"role": role, "content": content})
    return out


def ollama_native_tool_call_to_provider_chunk(
    tool_call: dict, synthetic_id: str,
) -> ProviderChunk:
    """Ollama /api/chat tool_call (already-parsed dict args, no id) ->
    ProviderChunk. We mint a synthetic tool_use_id so the coordinator's
    id-keyed pipeline keeps working; Ollama never sees the id again."""
    fn = tool_call.get("function", {}) or {}
    return ProviderChunk(
        kind="tool_use",
        tool_use_id=synthetic_id,
        tool_name=fn.get("name", "") or "",
        tool_input=fn.get("arguments", {}) or {},
    )


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
    )


# ----- Provider -----


class OllamaProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        thinking_enabled: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._thinking_enabled = thinking_enabled

    async def evict_model(self, model: str) -> None:
        """Force the daemon to unload `model` from VRAM.

        Ollama's native /api/generate accepts `keep_alive: 0` with an
        empty prompt as the documented eviction signal. Fire-and-forget
        from the caller's side: failures are swallowed (the daemon may
        not have the model loaded, may be unreachable, etc.); callers
        log a warning if they care.
        """
        if not model:
            return
        url = f"{self._base_url}/api/generate"
        async with httpx.AsyncClient(timeout=_CONTROL_TIMEOUT_S) as client:
            await client.post(url, json={"model": model, "keep_alive": 0})

    async def list_loaded_models(self) -> list[str]:
        """Return models currently resident in the daemon (via /api/ps).
        Raises RuntimeError on HTTP/parse failure so callers can decide
        whether to skip eviction or surface the error."""
        url = f"{self._base_url}/api/ps"
        async with httpx.AsyncClient(timeout=_CONTROL_TIMEOUT_S) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"ollama /api/ps returned {resp.status_code}: "
                    f"{extract_error_message(resp.text[:500])}"
                )
            body = resp.json()
            models = body.get("models") or []
            return [m.get("name") for m in models if isinstance(m, dict) and m.get("name")]

    async def list_models(self) -> list[str]:
        """List tool-capable models the daemon has pulled. Filters out
        models whose `/api/show` capabilities list does not include
        `tools` -- the agent loop requires tool calling, so non-capable
        models would only fail mid-turn. Returns sorted ids; raises
        RuntimeError on HTTP / parse failure so the API layer can
        surface a useful error to the UI."""
        url = f"{self._base_url}/v1/models"
        async with httpx.AsyncClient(timeout=_CONTROL_TIMEOUT_S) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"ollama /v1/models returned {resp.status_code}: "
                    f"{extract_error_message(resp.text[:500])}"
                )
            body = resp.json()
            data = body.get("data") or []
            ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
            ids = sorted(set(ids))
            return await self._filter_tool_capable(client, ids)

    async def _filter_tool_capable(
        self, client: "httpx.AsyncClient", ids: list[str],
    ) -> list[str]:
        """Return the subset of `ids` whose /api/show capabilities
        include `tools`. Best-effort: a model whose /api/show fails or
        omits capabilities is dropped (cannot prove tool support)."""
        if not ids:
            return []
        show_url = f"{self._base_url}/api/show"
        sem = asyncio.Semaphore(_LIST_MODELS_SHOW_CONCURRENCY)

        async def _capable(model_id: str) -> tuple[str, bool]:
            async with sem:
                try:
                    resp = await client.post(show_url, json={"name": model_id})
                except Exception as exc:
                    log.debug("ollama /api/show %s raised: %s", model_id, exc)
                    return model_id, False
            if resp.status_code != 200:
                log.debug("ollama /api/show %s -> %s", model_id, resp.status_code)
                return model_id, False
            try:
                body = resp.json()
            except Exception as exc:
                log.debug("ollama /api/show %s: bad json: %s", model_id, exc)
                return model_id, False
            caps = body.get("capabilities")
            if caps is None:
                log.warning(
                    "ollama /api/show for %s returned no `capabilities` field; "
                    "model will be hidden from the tool-capable list. "
                    "Upgrade Ollama if the dropdown is unexpectedly empty.",
                    model_id,
                )
                return model_id, False
            caps_lower = {str(c).lower() for c in caps}
            return model_id, "tools" in caps_lower

        results = await asyncio.gather(*(_capable(i) for i in ids))
        return [mid for mid, ok in results if ok]

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
        # Branch by thinking support. /v1/chat/completions (OpenAI-compat)
        # is the default; /api/chat (Ollama native) is required when the
        # caller asked for `think=true` since the compat layer ignores it.
        # `thinking=False` forces the compat path (verifier sub-runs) so no
        # <think> reasoning is generated or leaks into the verdict.
        if thinking is not False and self._thinking_enabled:
            inner = self._stream_native(
                system, messages, tools,
                transcript=transcript, round_index=round_index,
            )
        else:
            inner = self._stream_openai_compat(
                system, messages, tools,
                transcript=transcript, round_index=round_index,
            )
        # Some local models stream tool calls as prose -- recover them
        # transparently. XML shape handled unconditionally; the call-
        # syntax shape (name(args) / name{args}) needs the tool-name
        # set; positional-arg recovery (bare-JSON) needs ordered param
        # names from the input_schema.
        tool_names: set[str] = set()
        tool_schemas: dict[str, list[str]] = {}
        for t in tools or []:
            n = t.get("name")
            if not n:
                continue
            tool_names.add(n)
            params = _ordered_param_names(t.get("input_schema") or {})
            if params:
                tool_schemas[n] = params
        async for chunk in recover_inline_tool_calls(
            inner, tool_names=tool_names, tool_schemas=tool_schemas,
        ):
            yield chunk

    async def _stream_openai_compat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None,
        *,
        transcript: Transcript | None,
        round_index: int,
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

        await self._tx_request(transcript, round_index, body)

        url = f"{self._base_url}/v1/chat/completions"
        # Per-line accumulator for tool_call deltas: id+name arrive once
        # near the start, arguments stream as a concatenated string.
        # Indexed by `index` field in the OpenAI delta protocol.
        tool_call_buf: dict[int, dict] = {}
        # Harmony marker carry-buffers. See harmony_strip.py -- gemma4
        # and similar models leak `<|...|>` tokens into both visible
        # content AND the reasoning channel; each needs its own carry
        # because deltas interleave.
        text_carry: list[str] = []
        reason_carry: list[str] = []

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    await self._tx_wire(transcript, round_index, f"HTTP {resp.status_code}: {raw}")
                    raise RuntimeError(
                        f"ollama API error {resp.status_code}: {extract_error_message(raw)}"
                    )
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    # Capture every raw line BEFORE parsing so a crash
                    # downstream still leaves the byte trail behind.
                    await self._tx_wire(transcript, round_index, line)
                    # OpenAI SSE: each chunk is "data: {...}". A
                    # terminal "data: [DONE]" marks end of stream.
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"ollama: malformed SSE payload: {payload!r} ({exc})"
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
                    # Some Ollama models (e.g. nemotron-cascade) stream
                    # chain-of-thought into `reasoning` and never fill
                    # `content`. Surface as thinking so it lands in the
                    # transcript labeled; the coordinator drops these
                    # for the UI but counts the round.
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

        # Surface any held-back partial that never completed into a
        # marker, so user text containing literal `<` is not silently
        # dropped at stream end.
        tail = flush_harmony_carry(text_carry)
        if tail:
            yield ProviderChunk(kind="text", text=tail)
        reason_tail = flush_harmony_carry(reason_carry)
        if reason_tail:
            yield ProviderChunk(kind="thinking", text=reason_tail)

        # Emit accumulated tool_calls (if any) AFTER text streaming
        # completes -- matches Anthropic's "text first, tool_use last"
        # ordering that the coordinator's round_chunks reassembly
        # expects.
        for idx in sorted(tool_call_buf.keys()):
            yield openai_tool_call_to_provider_chunk(tool_call_buf[idx])

    async def _stream_native(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None,
        *,
        transcript: Transcript | None,
        round_index: int,
    ) -> AsyncIterator[ProviderChunk]:
        # Ollama /api/chat: takes a system message via leading {role:
        # "system"} entry like OpenAI, but tool messages drop tool_call_id
        # and tool_calls carry no id. NDJSON streaming (one full message
        # snapshot per line), not SSE.
        wire_messages: list[dict] = []
        if system:
            wire_messages.append({"role": "system", "content": system})
        wire_messages.extend(messages_anthropic_to_ollama_native(messages))

        body: dict[str, Any] = {
            "model": self._model,
            "messages": wire_messages,
            "stream": True,
            "think": True,
        }
        if tools:
            body["tools"] = tools_anthropic_to_openai(tools)

        await self._tx_request(transcript, round_index, body)

        url = f"{self._base_url}/api/chat"
        emitted_tool_calls: list[dict] = []
        # Same harmony carry as in the OpenAI-compat path; some local
        # models (gemma4) leak `<|...|>` markers through native chat
        # on both content AND thinking channels.
        text_carry: list[str] = []
        think_carry: list[str] = []

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status_code != 200:
                    raw = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    await self._tx_wire(transcript, round_index, f"HTTP {resp.status_code}: {raw}")
                    raise RuntimeError(
                        f"ollama API error {resp.status_code}: {extract_error_message(raw)}"
                    )
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    await self._tx_wire(transcript, round_index, line)
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"ollama: malformed NDJSON line: {line!r} ({exc})"
                        ) from exc
                    msg = evt.get("message") or {}
                    thinking = msg.get("thinking")
                    if thinking:
                        scrubbed = strip_harmony_text(thinking, think_carry)
                        if scrubbed:
                            yield ProviderChunk(kind="thinking", text=scrubbed)
                    content = msg.get("content")
                    if content:
                        scrubbed = strip_harmony_text(content, text_carry)
                        if scrubbed:
                            yield ProviderChunk(kind="text", text=scrubbed)
                    tcs = msg.get("tool_calls") or []
                    for tc in tcs:
                        emitted_tool_calls.append(tc)

        tail = flush_harmony_carry(text_carry)
        if tail:
            yield ProviderChunk(kind="text", text=tail)
        think_tail = flush_harmony_carry(think_carry)
        if think_tail:
            yield ProviderChunk(kind="thinking", text=think_tail)

        # /api/chat tool_calls carry no id. Mint a synthetic id per call
        # so the coordinator's tool_use_id pipeline keeps working; Ollama
        # never sees the id on subsequent turns.
        for i, tc in enumerate(emitted_tool_calls):
            synthetic_id = f"ollama-{round_index}-{i}"
            yield ollama_native_tool_call_to_provider_chunk(tc, synthetic_id)


def _ordered_param_names(input_schema: dict) -> list[str]:
    """Extract param names from a JSON Schema `input_schema`, ordered
    by `required` first (in declared order) then any remaining
    `properties` keys (dict insertion order). Used to drive
    positional-arg recovery in inline-recovery's BareJsonFlavor."""
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


