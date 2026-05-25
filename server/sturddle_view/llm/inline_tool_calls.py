"""Inline tool-call recovery for models that emit tool calls as prose.

Some local models (Qwen, Nemotron-3 variants) don't reliably follow
the OpenAI tool_call protocol. Instead they stream the call as text:

    <function=piece_at>
    <parameter=square>
    f3
    </parameter>
    </function>
    </tool_call>

The model intended to call a tool; we'd like to honor that intent
instead of leaking the literal XML into the user-visible panel.

This module wraps a ProviderChunk async iterator and:
- watches text chunks for the sentinel `<function=` (case-sensitive),
- buffers from the sentinel forward, suppressing those text chunks,
- on the closing `</function>` (and optional `</tool_call>`), parses
  the buffered XML into a synthetic `tool_use` chunk and yields it.

If parsing fails or the buffer never closes, the swallowed text is
flushed back as a normal text chunk -- the user sees what the model
emitted instead of silent loss.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import AsyncIterator

from .base import ProviderChunk


log = logging.getLogger(__name__)


_SENTINEL = "<function="
# UUID hex slice length for synthetic tool_use_id (`inline-XXXXXXXXXXXX`).
# 12 chars of hex = 48 bits of entropy, plenty for collision-free IDs
# within a single turn. Env override is for ops if a longer ID is ever
# needed for log correlation.
_DEFAULT_INLINE_ID_LEN = 12
INLINE_ID_LEN = int(os.environ.get("SV_AI_INLINE_TOOL_ID_LEN", _DEFAULT_INLINE_ID_LEN))
# Matches the full tool-call XML. The <tool_call> trailer is optional
# (some models emit it, some don't).
_TOOL_CALL_RE = re.compile(
    r"<function=(?P<name>[A-Za-z_][A-Za-z0-9_]*)>\s*"
    r"(?P<body>.*?)"
    r"</function>\s*"
    r"(?:</tool_call>\s*)?",
    re.DOTALL,
)
_PARAM_RE = re.compile(
    r"<parameter=(?P<key>[A-Za-z_][A-Za-z0-9_]*)>\s*"
    r"(?P<val>.*?)"
    r"\s*</parameter>",
    re.DOTALL,
)


def _coerce_param(raw: str) -> object:
    """Coerce a parameter value string. Bare numbers / bools / lists
    parse as JSON; everything else stays a string. Matters because the
    XML protocol is type-blind but tool schemas often require ints
    (e.g. `analyze(depth=12)`)."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _parse_tool_call(xml: str) -> tuple[str, dict[str, object], int] | None:
    """Return (name, input, end_offset) or None on parse failure.
    `end_offset` is the position just past the matched XML, so the
    caller can slice any trailing prose without re-searching."""
    m = _TOOL_CALL_RE.search(xml)
    if not m:
        return None
    body = m.group("body")
    params: dict[str, object] = {}
    for pm in _PARAM_RE.finditer(body):
        params[pm.group("key")] = _coerce_param(pm.group("val").strip())
    return m.group("name"), params, m.end()


def _synthesize_tool_use(name: str, params: dict[str, object]) -> ProviderChunk:
    return ProviderChunk(
        kind="tool_use",
        tool_use_id=f"inline-{uuid.uuid4().hex[:INLINE_ID_LEN]}",
        tool_name=name,
        tool_input=params,
    )


async def recover_inline_tool_calls(
    upstream: AsyncIterator[ProviderChunk],
) -> AsyncIterator[ProviderChunk]:
    """Async-iterator wrapper that converts inline-XML tool calls into
    synthetic tool_use chunks. Non-text chunks pass through unchanged."""
    buf = ""  # accumulating since the sentinel was seen
    capturing = False

    async for chunk in upstream:
        if chunk.kind != "text" or not chunk.text:
            yield chunk
            continue

        if not capturing:
            idx = chunk.text.find(_SENTINEL)
            if idx < 0:
                yield chunk
                continue
            # Split: emit any prefix as normal text, start capturing
            # from the sentinel.
            if idx > 0:
                yield ProviderChunk(kind="text", text=chunk.text[:idx])
            buf = chunk.text[idx:]
            capturing = True
        else:
            buf += chunk.text

        # Try to parse whatever's in the buffer. If we have a complete
        # tool-call shape, emit it and exit capturing.
        result = _parse_tool_call(buf)
        if result is None:
            # Not complete yet; keep buffering.
            continue
        name, params, end = result
        # Anything after the parsed XML is post-call prose; rare. NOTE:
        # back-to-back tool calls in one chunk would emit the second's
        # XML as plain text (we re-enter the loop only on next chunk).
        # Hasn't been observed in real transcripts; fix when it does.
        tail = buf[end:]
        log.info("inline-XML tool call recovered: %s(%s)", name, params)
        yield _synthesize_tool_use(name, params)
        if tail:
            yield ProviderChunk(kind="text", text=tail)
        buf = ""
        capturing = False

    # Stream ended mid-capture: parsing never completed. Flush the
    # buffered text so the user sees what the model produced instead
    # of silent loss.
    if capturing and buf:
        log.warning(
            "inline-XML tool call did not close before stream end; flushing %d bytes",
            len(buf),
        )
        yield ProviderChunk(kind="text", text=buf)
