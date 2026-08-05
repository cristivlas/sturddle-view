"""Ollama provider.

Speaks two wire formats. The OpenAI-compatible chat-completions path is
the default and shares its translation + SSE loop with every other
OpenAI-compat provider via `openai_compat` (canonical = Anthropic, spec
§Providers). The native `/api/chat` path is Ollama-specific and used only
when `think=true` is requested (the compat layer ignores thinking).

Endpoints: `{base_url}/v1/chat/completions` (compat) and
`{base_url}/api/chat` (native). Both stream; both translate to/from the
canonical Anthropic message/tool shape.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

from ._errors import extract_error_message
from .base import LLMProvider, Message, ProviderChunk, ProviderUsage, ToolWireSpec
from .harmony_strip import flush_harmony_carry, strip_harmony_text
from .inline_recovery import recover_inline_tool_calls
from .openai_compat import (
    inline_recovery_args,
    malformed_tool_args_detail,
    messages_anthropic_to_openai,
    openai_tool_call_to_provider_chunk,
    stream_openai_compat,
    tools_anthropic_to_openai,
)
from .transcript import Transcript


log = logging.getLogger(__name__)

# Re-exported for back-compat: tests and callers import these from the
# ollama module. The implementations now live in openai_compat.
__all__ = [
    "DEFAULT_BASE_URL",
    "OllamaProvider",
    "malformed_tool_args_detail",
    "messages_anthropic_to_openai",
    "openai_tool_call_to_provider_chunk",
    "tools_anthropic_to_openai",
]


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

# ----- Native (/api/chat) translation helpers ------------------------
# The OpenAI-compat translation lives in openai_compat.py (shared). The
# helpers below are Ollama-native: tool messages carry no tool_call_id
# and tool_calls use dict arguments + no id.


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


def _usage_from_native(evt: dict) -> ProviderUsage | None:
    """Map an /api/chat NDJSON line's token counts to ProviderUsage.

    prompt_eval_count / eval_count ride the final (done:true) line; None
    when the line carries neither. No cache accounting on this surface
    (local KV-cache reuse is free anyway)."""
    prompt = evt.get("prompt_eval_count")
    completion = evt.get("eval_count")
    if not isinstance(prompt, int) and not isinstance(completion, int):
        return None
    return ProviderUsage(
        input_tokens=prompt if isinstance(prompt, int) else 0,
        output_tokens=completion if isinstance(completion, int) else 0,
    )


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


# ----- Provider -----


class OllamaProvider(LLMProvider):
    provider_name = "ollama"

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
                    f"{extract_error_message(resp.text)}"
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
                    f"{extract_error_message(resp.text)}"
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
        force_tool_call: bool = False,
    ) -> AsyncIterator[ProviderChunk]:
        # Branch by thinking support. /v1/chat/completions (OpenAI-compat)
        # is the default; /api/chat (Ollama native) is required when the
        # caller asked for `think=true` since the compat layer ignores it.
        # `thinking=False` forces the compat path (verifier sub-runs) so no
        # <think> reasoning is generated or leaks into the verdict.
        # `force_tool_call` only reaches the compat path -- /api/chat has no
        # tool_choice, and the verifier (the only forcing caller) always
        # runs thinking-off, i.e. compat. Best-effort either way: local
        # models may ignore it, and the coordinator's nudge backstops.
        if thinking is not False and self._thinking_enabled:
            inner = self._stream_native(
                system, messages, tools,
                transcript=transcript, round_index=round_index,
            )
        else:
            inner = self._stream_openai_compat(
                system, messages, tools,
                transcript=transcript, round_index=round_index,
                force_tool_call=force_tool_call,
            )
        # Some local models stream tool calls as prose -- recover them
        # transparently. XML shape handled unconditionally; call-syntax
        # and positional bare-JSON shapes need the tool names + ordered
        # param names that inline_recovery_args derives from the schema.
        tool_names, tool_schemas = inline_recovery_args(tools)
        async for chunk in recover_inline_tool_calls(
            inner, tool_names=tool_names, tool_schemas=tool_schemas,
        ):
            yield chunk

    def _stream_openai_compat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None,
        *,
        transcript: Transcript | None,
        round_index: int,
        force_tool_call: bool = False,
    ) -> AsyncIterator[ProviderChunk]:
        # Assemble OpenAI-shaped request. System prompt is a separate
        # first message in OpenAI's API; coordinator passes it as a
        # bare string so we wrap it here. The SSE loop + translation back
        # to ProviderChunks is shared (openai_compat).
        wire_messages: list[dict] = []
        if system:
            wire_messages.append({"role": "system", "content": system})
        wire_messages.extend(messages_anthropic_to_openai(messages))

        body: dict[str, Any] = {
            "model": self._model,
            "messages": wire_messages,
            "stream": True,
            # Ask for the usage-bearing final chunk (OpenAI semantics).
            # Ollama versions predating stream_options ignore unknown
            # request fields, so this degrades to no usage chunk.
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools_anthropic_to_openai(tools)
            if force_tool_call:
                # OpenAI-compat spelling of "must call a tool this round"
                # (verifier first rounds). See LLMProvider.stream().
                body["tool_choice"] = "required"

        return stream_openai_compat(
            self,
            url=f"{self._base_url}/v1/chat/completions",
            body=body,
            headers={"Content-Type": "application/json"},
            error_label="ollama",
            transcript=transcript,
            round_index=round_index,
        )

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
        # /api/chat always reports token counts on its final (done:true)
        # line -- prompt_eval_count / eval_count. Last seen wins.
        usage: ProviderUsage | None = None
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
                    raw = (await resp.aread()).decode("utf-8", errors="replace")
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
                    mapped = _usage_from_native(evt)
                    if mapped is not None:
                        usage = mapped

        tail = flush_harmony_carry(text_carry)
        if tail:
            yield ProviderChunk(kind="text", text=tail)
        think_tail = flush_harmony_carry(think_carry)
        if think_tail:
            yield ProviderChunk(kind="thinking", text=think_tail)

        # Usage before tool_use -- same ordering contract as the other
        # providers: the coordinator stops consuming at the first tool_use.
        if usage is not None:
            yield ProviderChunk(kind="usage", usage=usage)

        # /api/chat tool_calls carry no id. Mint a synthetic id per call
        # so the coordinator's tool_use_id pipeline keeps working; Ollama
        # never sees the id on subsequent turns.
        for i, tc in enumerate(emitted_tool_calls):
            synthetic_id = f"ollama-{round_index}-{i}"
            yield ollama_native_tool_call_to_provider_chunk(tc, synthetic_id)


