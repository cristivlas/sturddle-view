"""Ollama provider.

Speaks two wire formats. The OpenAI-compatible chat-completions path is
the default and shares its translation + SSE loop with every other
OpenAI-compat provider via `openai_compat` (canonical = Anthropic, see the
spec's Providers section). The native `/api/chat` path is Ollama-specific
and used only when `think=true` is requested; thinking-off requests go
through compat with `reasoning_effort: "none"`, except for models a
one-time probe finds leaking reasoning into visible text.

Endpoints: `{base_url}/v1/chat/completions` (compat) and
`{base_url}/api/chat` (native). Both stream; both translate to/from the
canonical Anthropic message/tool shape.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing
from http import HTTPStatus
from typing import Any, AsyncIterator

import httpx

from ..config import PROVIDER_OLLAMA
from ..env_utils import env_int
from ._errors import ThinkingUnsupported, extract_error_message, is_thinking_unsupported
from .base import (
    CONTROL_TIMEOUT_S,
    JSON_HEADERS,
    LLMProvider,
    Message,
    ProviderChunk,
    ProviderUsage,
    ToolWireSpec,
    with_system_message,
)
from .harmony_strip import flush_harmony_carry, strip_harmony_text
from .inline_recovery import recover_inline_tool_calls
from .openai_compat import (
    REASONING_EFFORT_KEY,
    inline_recovery_args,
    messages_anthropic_to_openai,
    stream_openai_compat,
    tools_anthropic_to_openai,
)
from .transcript import Transcript


log = logging.getLogger(__name__)


DEFAULT_BASE_URL = "http://localhost:11434"

# Max parallel /api/show calls during list_models capability filtering.
# Bounds simultaneous load on the local daemon when a user has many
# models pulled. Cheap calls (single-digit ms each); the cap mostly
# keeps things polite.
_LIST_MODELS_SHOW_CONCURRENCY = 8

# Thinking-capable models (e.g. granite4.2) reason by default when the
# request is silent; "none" switches that off on the compat path.
_REASONING_EFFORT_OFF = "none"
# Native /api/chat request field enabling thinking.
_THINK_KEY = "think"
# Request-body fields shared by the compat and native endpoints.
_MODEL_KEY = "model"
_MESSAGES_KEY = "messages"
_STREAM_KEY = "stream"
_TOOLS_KEY = "tools"

# /api/show capability names.
_CAP_TOOLS = "tools"
_CAP_THINKING = "thinking"

# Some thinking-only models ignore "none" and reason into visible text,
# ending it with a bare `</think>` (the template opened the block). A
# one-time probe per model detects this; leaky models get no "none".
_THINK_CLOSE_TAG = "</think>"
_THINK_PROBE_PROMPT = "Reply with only: OK"
_DEFAULT_THINK_PROBE_MAX_TOKENS = 512
_THINK_PROBE_MAX_TOKENS = env_int(
    "SV_OLLAMA_THINK_PROBE_MAX_TOKENS", _DEFAULT_THINK_PROBE_MAX_TOKENS, min_value=1,
)
# (base_url, model) -> leaks. Process-lifetime; a restart re-probes.
_think_leak_cache: dict[tuple[str, str], bool] = {}
_think_probe_locks: dict[tuple[str, str], asyncio.Lock] = {}

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


class _ThinkCloseWatch:
    """Spots `</think>` in streamed text, including across delta splits."""

    def __init__(self) -> None:
        self._tail = ""

    def feed(self, text: str) -> bool:
        buf = self._tail + text
        if _THINK_CLOSE_TAG in buf:
            return True
        self._tail = buf[-(len(_THINK_CLOSE_TAG) - 1):]
        return False


async def _flag_think_leak(
    inner: AsyncIterator[ProviderChunk], key: tuple[str, str],
) -> AsyncIterator[ProviderChunk]:
    """Pass-through that marks `key` leaky if a reasoning-off reply still
    carries `</think>` -- backstop for a probe that missed it."""
    watch = _ThinkCloseWatch()
    flagged = False
    async for chunk in inner:
        if not flagged and chunk.kind == "text" and chunk.text and watch.feed(chunk.text):
            flagged = True
            _think_leak_cache[key] = True
            log.warning(
                "%s: %s wrote %s with reasoning off; no longer disabling it",
                PROVIDER_OLLAMA, key[1], _THINK_CLOSE_TAG,
            )
        yield chunk


# ----- Provider -----


class OllamaProvider(LLMProvider):
    provider_name = PROVIDER_OLLAMA

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
        empty prompt as the documented eviction signal. Transport errors
        propagate (the daemon may be unreachable); callers treat eviction
        as best-effort and log them.
        """
        if not model:
            return
        url = f"{self._base_url}/api/generate"
        async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT_S) as client:
            await client.post(url, json={_MODEL_KEY: model, "keep_alive": 0})

    async def list_loaded_models(self) -> list[str]:
        """Return models currently resident in the daemon (via /api/ps).
        Raises RuntimeError on HTTP/parse failure so callers can decide
        whether to skip eviction or surface the error."""
        url = f"{self._base_url}/api/ps"
        async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT_S) as client:
            resp = await client.get(url)
            if resp.status_code != HTTPStatus.OK:
                raise RuntimeError(
                    f"{PROVIDER_OLLAMA} /api/ps returned {resp.status_code}: "
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
        async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT_S) as client:
            resp = await client.get(url)
            if resp.status_code != HTTPStatus.OK:
                raise RuntimeError(
                    f"{PROVIDER_OLLAMA} /v1/models returned {resp.status_code}: "
                    f"{extract_error_message(resp.text)}"
                )
            body = resp.json()
            data = body.get("data") or []
            ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
            ids = sorted(set(ids))
            return await self._filter_tool_capable(client, ids)

    async def _filter_tool_capable(
        self, client: httpx.AsyncClient, ids: list[str],
    ) -> list[str]:
        """Return the subset of `ids` whose /api/show capabilities
        include `tools`. Best-effort: a model whose /api/show fails or
        omits capabilities is dropped (cannot prove tool support)."""
        if not ids:
            return []
        sem = asyncio.Semaphore(_LIST_MODELS_SHOW_CONCURRENCY)

        async def _capable(model_id: str) -> tuple[str, bool]:
            async with sem:
                caps = await self._show_capabilities(client, model_id)
            return model_id, caps is not None and _CAP_TOOLS in caps

        results = await asyncio.gather(*(_capable(i) for i in ids))
        return [mid for mid, ok in results if ok]

    async def _show_capabilities(
        self, client: httpx.AsyncClient, model_id: str,
    ) -> set[str] | None:
        """Lower-cased /api/show capabilities of `model_id`; None when the
        call fails or the daemon omits the field (capabilities unknown)."""
        show_url = f"{self._base_url}/api/show"
        try:
            resp = await client.post(show_url, json={"name": model_id})
        except Exception as exc:
            log.debug("%s /api/show %s raised: %s", PROVIDER_OLLAMA, model_id, exc)
            return None
        if resp.status_code != HTTPStatus.OK:
            log.debug("%s /api/show %s -> %s", PROVIDER_OLLAMA, model_id, resp.status_code)
            return None
        try:
            body = resp.json()
        except Exception as exc:
            log.debug("%s /api/show %s: bad json: %s", PROVIDER_OLLAMA, model_id, exc)
            return None
        caps = body.get("capabilities")
        if caps is None:
            log.warning(
                "%s /api/show for %s returned no `capabilities` field; "
                "treating its capabilities as unknown (it will not be "
                "listed as tool-capable). Upgrade Ollama.",
                PROVIDER_OLLAMA, model_id,
            )
            return None
        return {str(c).lower() for c in caps}

    async def _leaks_think_when_off(self) -> bool:
        """True when the model ignores reasoning "none" and reasons into
        visible text. Probed once per (base_url, model); a failed probe is
        logged, not cached, and reads as not leaky."""
        key = (self._base_url, self._model)
        cached = _think_leak_cache.get(key)
        if cached is not None:
            return cached
        async with _think_probe_locks.setdefault(key, asyncio.Lock()):
            cached = _think_leak_cache.get(key)
            if cached is not None:
                return cached
            try:
                leaks = await self._probe_think_leak()
            except Exception as exc:
                log.warning(
                    "%s: think-leak probe failed for %s; disabling reasoning: %s",
                    PROVIDER_OLLAMA, self._model, exc,
                )
                return False
            _think_leak_cache[key] = leaks
            log.info(
                "%s: %s think-leak probe: %s",
                PROVIDER_OLLAMA, self._model, "leaks" if leaks else "clean",
            )
            return leaks

    async def _probe_think_leak(self) -> bool:
        """One tiny reasoning-off request; leaky iff `</think>` shows up in
        the visible text. Stops reading as soon as it does."""
        async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT_S) as client:
            caps = await self._show_capabilities(client, self._model)
        if caps is not None and _CAP_THINKING not in caps:
            return False
        body: dict[str, Any] = {
            _MODEL_KEY: self._model,
            _MESSAGES_KEY: [{"role": "user", "content": _THINK_PROBE_PROMPT}],
            _STREAM_KEY: True,
            "max_tokens": _THINK_PROBE_MAX_TOKENS,
            REASONING_EFFORT_KEY: _REASONING_EFFORT_OFF,
        }
        watch = _ThinkCloseWatch()
        async with aclosing(stream_openai_compat(
            self,
            url=f"{self._base_url}/v1/chat/completions",
            body=body,
            headers=JSON_HEADERS,
            error_label=PROVIDER_OLLAMA,
            transcript=None,
            round_index=0,
        )) as chunks:
            async for chunk in chunks:
                if chunk.kind == "text" and chunk.text and watch.feed(chunk.text):
                    return True
        return False

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
        # Thinking on => /api/chat (Ollama native) with `think=true`; off
        # => /v1/chat/completions (OpenAI-compat) with reasoning disabled,
        # unless the model leaks it (then its reasoning channel is dropped).
        # `thinking=False` forces the off path (verifier sub-runs).
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
            reasoning_off = not await self._leaks_think_when_off()
            inner = self._stream_openai_compat(
                system, messages, tools,
                transcript=transcript, round_index=round_index,
                force_tool_call=force_tool_call,
                reasoning_off=reasoning_off,
            )
            if reasoning_off:
                inner = _flag_think_leak(inner, (self._base_url, self._model))
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
        reasoning_off: bool,
        force_tool_call: bool = False,
    ) -> AsyncIterator[ProviderChunk]:
        # Assemble OpenAI-shaped request. System prompt is a separate
        # first message in OpenAI's API; coordinator passes it as a
        # bare string so we wrap it here. The SSE loop + translation back
        # to ProviderChunks is shared (openai_compat).
        body: dict[str, Any] = {
            _MODEL_KEY: self._model,
            _MESSAGES_KEY: with_system_message(system, messages_anthropic_to_openai(messages)),
            _STREAM_KEY: True,
            # Ask for the usage-bearing final chunk (OpenAI semantics).
            # Ollama versions predating stream_options ignore unknown
            # request fields, so this degrades to no usage chunk.
            "stream_options": {"include_usage": True},
        }
        if reasoning_off:
            body[REASONING_EFFORT_KEY] = _REASONING_EFFORT_OFF
        if tools:
            body[_TOOLS_KEY] = tools_anthropic_to_openai(tools)
            if force_tool_call:
                # OpenAI-compat spelling of "must call a tool this round"
                # (verifier first rounds). See LLMProvider.stream().
                body["tool_choice"] = "required"

        return stream_openai_compat(
            self,
            url=f"{self._base_url}/v1/chat/completions",
            body=body,
            headers=JSON_HEADERS,
            error_label=PROVIDER_OLLAMA,
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
        body: dict[str, Any] = {
            _MODEL_KEY: self._model,
            _MESSAGES_KEY: with_system_message(system, messages_anthropic_to_ollama_native(messages)),
            _STREAM_KEY: True,
            _THINK_KEY: True,
        }
        if tools:
            body[_TOOLS_KEY] = tools_anthropic_to_openai(tools)

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
                headers=JSON_HEADERS,
            ) as resp:
                if resp.status_code != HTTPStatus.OK:
                    raw = (await resp.aread()).decode("utf-8", errors="replace")
                    await self._tx_wire(transcript, round_index, f"HTTP {resp.status_code}: {raw}")
                    # Reaching this path means the body carried think:true, so
                    # a bad request naming thinking is the model refusing it.
                    # Restate it in our own words off self._model, so the UI
                    # never echoes provider phrasing back to the user; the raw
                    # body is already in the transcript above.
                    if is_thinking_unsupported(resp.status_code, raw):
                        raise ThinkingUnsupported(
                            f'"{self._model}" does not support extended thinking'
                        )
                    raise RuntimeError(
                        f"{PROVIDER_OLLAMA} API error {resp.status_code}: "
                        f"{extract_error_message(raw)}"
                    )
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    await self._tx_wire(transcript, round_index, line)
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"{PROVIDER_OLLAMA}: malformed NDJSON line: {line!r} ({exc})"
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
            synthetic_id = f"{PROVIDER_OLLAMA}-{round_index}-{i}"
            yield ollama_native_tool_call_to_provider_chunk(tc, synthetic_id)
