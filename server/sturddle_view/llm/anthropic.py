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
- `content_block_stop` -- closes the block. Tool_use chunks are built
  now (after we have the full input JSON) but held until `message_stop`.
- `message_start` / `message_delta` -- carry the `usage` accounting
  (input + cache fields up front, final output_tokens in the delta).
- `message_stop` -- terminal; we yield one `usage` chunk, then any held
  tool_use chunks. The order matters: the coordinator stops consuming
  at the first tool_use, so usage must precede it.

Errors mid-stream arrive as `event: error` with a JSON envelope. HTTP
errors before the stream opens come back as a non-200 status.
"""
from __future__ import annotations

import json
import logging
import re
from http import HTTPStatus
from typing import Any, AsyncIterator

import httpx

from ..config import PROVIDER_ANTHROPIC
from ..env_utils import env_int
from ._errors import extract_error_message
from .base import (
    CONTROL_TIMEOUT_S,
    JSON_HEADERS,
    LLMProvider,
    Message,
    ProviderChunk,
    ProviderUsage,
    ToolWireSpec,
)
from .transcript import Transcript


log = logging.getLogger(__name__)


_ANTHROPIC_BASE = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_MODELS_PATH = "/v1/models"
_MESSAGES_PATH = "/v1/messages"
_SSE_DATA_PREFIX = "data:"
_BUDGET_TOKENS_KEY = "budget_tokens"
# Spec doesn't constrain max_tokens; pick a generous cap so the model
# rarely truncates a coaching prose response. Env override for ops.
_DEFAULT_MAX_TOKENS = 4096
_MAX_TOKENS = env_int("SV_AI_MAX_TOKENS", _DEFAULT_MAX_TOKENS, min_value=1)
# Name fallback: Opus from this version on is adaptive-thinking only.
_ADAPTIVE_THINKING_MIN_MAJOR = 4
_ADAPTIVE_THINKING_MIN_MINOR = 6

# Thinking wire shapes, resolved from the Models API capability tree
# (capabilities.thinking.types.{adaptive,enabled}.supported).
THINKING_ADAPTIVE = "adaptive"   # {"type": "adaptive"}
THINKING_EXTENDED = "extended"   # {"type": "enabled", "budget_tokens": N}
THINKING_NONE = "none"           # model takes no thinking config -- omit

# model id -> mode; capabilities are static per id so process-lifetime
# caching is safe. Seeded by list_models(), extended by stream();
# fallback guesses are never cached.
_thinking_mode_cache: dict[str, str] = {}

# Usage fields copied off the wire; names match ProviderUsage fields.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _merge_usage(dst: dict[str, int], src: Any) -> None:
    """Fold a wire `usage` dict into `dst`, keeping only known int
    fields. message_start carries input + cache counts; message_delta
    re-states output_tokens cumulatively, so later merges win."""
    if not isinstance(src, dict):
        return
    for key in _USAGE_KEYS:
        val = src.get(key)
        if isinstance(val, int):
            dst[key] = val


def _thinking_mode_from_capabilities(model_obj: dict) -> str | None:
    """Resolve a model's thinking mode from a /v1/models item.

    Returns None when the payload carries no capability tree (older
    API surface) so callers can fall back.
    """
    caps = model_obj.get("capabilities")
    if not isinstance(caps, dict):
        return None
    types = (caps.get("thinking") or {}).get("types") or {}
    if (types.get("adaptive") or {}).get("supported"):
        return THINKING_ADAPTIVE
    if (types.get("enabled") or {}).get("supported"):
        return THINKING_EXTENDED
    return THINKING_NONE


def _thinking_mode_fallback(model: str) -> str:
    """Heuristic used only when the Models API is unreachable.

    Opus >= 4.6 and the Fable/Mythos families are adaptive-only;
    everything else takes the extended (budget_tokens) shape.
    """
    if re.match(r"claude-(fable|mythos)-", model or ""):
        return THINKING_ADAPTIVE
    m = re.match(r"claude-opus-(\d+)-(\d+)", model or "")
    if m and (int(m.group(1)), int(m.group(2))) >= (
        _ADAPTIVE_THINKING_MIN_MAJOR, _ADAPTIVE_THINKING_MIN_MINOR
    ):
        return THINKING_ADAPTIVE
    return THINKING_EXTENDED


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
                    f"{PROVIDER_ANTHROPIC}: tool {self.tool_name!r} arguments not valid "
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


def _held_chunks(
    usage_fields: dict[str, int], pending_tools: list[ProviderChunk],
) -> list[ProviderChunk]:
    """End-of-stream flush: usage first (the coordinator stops consuming at
    the first tool_use), then the held tool_use chunks."""
    if not usage_fields:
        return list(pending_tools)
    return [ProviderChunk(kind="usage", usage=ProviderUsage(**usage_fields)), *pending_tools]


class AnthropicProvider(LLMProvider):
    provider_name = PROVIDER_ANTHROPIC

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

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            **JSON_HEADERS,
        }

    async def _get_json(self, path: str) -> Any:
        """GET a control-plane endpoint; RuntimeError on non-200."""
        async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT_S) as client:
            resp = await client.get(f"{_ANTHROPIC_BASE}{path}", headers=self._headers())
        if resp.status_code != HTTPStatus.OK:
            raise RuntimeError(
                f"{PROVIDER_ANTHROPIC} {path} returned {resp.status_code}: "
                f"{extract_error_message(resp.text)}"
            )
        return resp.json()

    async def _resolve_thinking_mode(self) -> str:
        """Thinking mode for the configured model, capability-driven.

        Cache hit (seeded by list_models() or a prior call) is free;
        a miss fetches GET /v1/models/{id}. Fetch failure falls back to
        the name heuristic without caching it.
        """
        cached = _thinking_mode_cache.get(self._model)
        if cached is not None:
            return cached
        try:
            mode = _thinking_mode_from_capabilities(
                await self._get_json(f"{_MODELS_PATH}/{self._model}")
            )
        except Exception as e:
            log.warning(
                "%s: capability lookup failed for %s; using name heuristic: %s",
                PROVIDER_ANTHROPIC, self._model, e,
            )
            return _thinking_mode_fallback(self._model)
        if mode is None:
            return _thinking_mode_fallback(self._model)
        _thinking_mode_cache[self._model] = mode
        return mode

    def _thinking_param(self, mode: str) -> dict | None:
        if mode == THINKING_ADAPTIVE:
            return {"type": "adaptive"}
        if mode == THINKING_EXTENDED:
            return {"type": "enabled", _BUDGET_TOKENS_KEY: self._thinking_budget_tokens}
        return None

    async def list_models(self) -> list[str]:
        """List available Anthropic models via GET /v1/models.

        Requires the user's API key. Raises RuntimeError on
        auth / network failure so the API layer can surface a useful
        error to the UI. Side effect: seeds the thinking-mode cache
        from each item's capability tree for thinking_modes().
        """
        self._require_api_key(self._api_key)
        body = await self._get_json(_MODELS_PATH)
        data = body.get("data") or []
        ids = []
        for m in data:
            if not (isinstance(m, dict) and m.get("id")):
                continue
            ids.append(m["id"])
            mode = _thinking_mode_from_capabilities(m)
            if mode is not None:
                _thinking_mode_cache[m["id"]] = mode
        return sorted(set(ids))

    def thinking_modes(self, models: list[str]) -> dict[str, str]:
        """Per-model thinking mode for the Settings UI.

        Cache entries come from list_models(); ids the capability tree
        didn't cover resolve via the name heuristic.
        """
        return {
            m: _thinking_mode_cache.get(m) or _thinking_mode_fallback(m)
            for m in models
        }

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
        self._require_api_key(self._api_key)

        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": _MAX_TOKENS,
            "messages": messages,
            "stream": True,
            # Top-level auto-caching: places one cache breakpoint on the
            # last cacheable block, so each agent round reads the prior
            # round's full prefix (tools + system + history) at ~0.1x
            # instead of re-billing it. Prefixes below the model's
            # cacheable minimum silently don't cache -- no error.
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
            if force_tool_call:
                # Verifier first rounds: a tool call is structurally
                # required, so the no-tool-verdict nudge round never runs.
                # Sequential: one call per round (coordinator loop is v1
                # sequential). Caller guarantees thinking is off -- the
                # API rejects forced tool choice with thinking enabled.
                body["tool_choice"] = {
                    "type": "any",
                    "disable_parallel_tool_use": True,
                }
        # `thinking=False` forces it off for this call (verifier sub-runs).
        if thinking is not False and self._thinking_enabled:
            param = self._thinking_param(await self._resolve_thinking_mode())
            if param is not None:
                body["thinking"] = param
                # Anthropic requires max_tokens > budget_tokens; lift the
                # cap so visible output isn't squeezed by reasoning.
                if _BUDGET_TOKENS_KEY in param:
                    body["max_tokens"] = _MAX_TOKENS + self._thinking_budget_tokens

        log.info(
            "%s stream: model=%s thinking=%s",
            PROVIDER_ANTHROPIC,
            self._model,
            body.get("thinking"),
        )
        await self._tx_request(transcript, round_index, body)

        # Index -> open block state. For text/thinking we just remember
        # the kind so deltas route correctly; for tool_use we keep a full
        # accumulator, build the chunk at content_block_stop, and hold it
        # in `pending_tools` until message_stop -- the usage chunk must go
        # first (the coordinator stops consuming at the first tool_use).
        open_text_kinds: dict[int, str] = {}
        open_tools: dict[int, _ToolUseAccumulator] = {}
        pending_tools: list[ProviderChunk] = []
        usage_fields: dict[str, int] = {}
        flushed = False

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", f"{_ANTHROPIC_BASE}{_MESSAGES_PATH}", json=body, headers=self._headers(),
            ) as resp:
                if resp.status_code != HTTPStatus.OK:
                    raw = (await resp.aread()).decode("utf-8", errors="replace")
                    await self._tx_wire(transcript, round_index, f"HTTP {resp.status_code}: {raw}")
                    raise RuntimeError(
                        f"{PROVIDER_ANTHROPIC} API error {resp.status_code}: "
                        f"{extract_error_message(raw)}"
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
                    if not line.startswith(_SSE_DATA_PREFIX):
                        continue
                    payload = line[len(_SSE_DATA_PREFIX):].strip()
                    if not payload:
                        continue
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"{PROVIDER_ANTHROPIC}: malformed SSE payload: {payload!r} ({exc})"
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
                            pending_tools.append(acc.to_chunk())
                        open_text_kinds.pop(idx, None)
                    elif etype == "message_start":
                        _merge_usage(
                            usage_fields, (evt.get("message") or {}).get("usage")
                        )
                    elif etype == "message_delta":
                        _merge_usage(usage_fields, evt.get("usage"))
                    elif etype == "message_stop":
                        for chunk in _held_chunks(usage_fields, pending_tools):
                            yield chunk
                        flushed = True
                    elif etype == "error":
                        err = evt.get("error") or {}
                        msg = err.get("message") or json.dumps(err)
                        raise RuntimeError(f"{PROVIDER_ANTHROPIC} stream error: {msg}")
                    # ping carries no chunk-relevant data for us.
                # Defensive: a stream that closed without message_stop still
                # surfaces whatever was held back.
                if not flushed:
                    for chunk in _held_chunks(usage_fields, pending_tools):
                        yield chunk
