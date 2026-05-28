"""Strip markdown emphasis markers from streamed text.

Some models wrap chess content in markdown (`**f2**`, `__Qxh7+__`,
`` `Nf3` ``). The validation layer reads prose for square/piece
references; markdown around them hides the content. Inline tool-call
recovery has the same problem when models wrap calls in `**...**`.

This module strips paired emphasis markers (`**`, `__`, `` ` ``) so
both downstream consumers see clean text. Single `*` and `_` are NOT
stripped -- they have legitimate uses (single-char emphasis, snake_case
identifiers in tool args) and aren't a problem for our use cases.

Streaming-aware: `strip_markdown(delta, carry)` mirrors the harmony
strip pattern so a marker split across deltas is still stripped on
the next chunk.
"""
from __future__ import annotations

from dataclasses import replace
from typing import AsyncIterator

from .base import ProviderChunk


_MARKERS = ("**", "__", "`")


def strip_markdown(delta: str, carry: list[str]) -> str:
    """Return `delta` with markdown emphasis markers removed.

    `carry` is a one-element list owned by the caller, holding any
    trailing partial-marker char from the previous call. On entry the
    leftover is prepended; on return `carry[0]` holds the new
    trailing partial (e.g. a lone `*` that may complete to `**`)."""
    buf = (carry[0] if carry else "") + delta
    out = buf
    for marker in _MARKERS:
        out = out.replace(marker, "")
    # Hold back a trailing `*` or `_` (single char) that could complete
    # into a `**` or `__` on the next chunk. Backtick markers are
    # single-char so they never get held.
    hold = ""
    if out.endswith("*") or out.endswith("_"):
        hold = out[-1]
        out = out[:-1]
    if carry:
        carry[0] = hold
    else:
        carry.append(hold)
    return out


def flush_markdown_carry(carry: list[str]) -> str:
    """Return whatever the carry buffer still holds, then clear it.

    Called at stream end so a held partial that never completed does
    not silently swallow user-visible text."""
    if not carry:
        return ""
    held = carry[0]
    carry[0] = ""
    return held


async def strip_markdown_stream(
    inner: AsyncIterator[ProviderChunk],
) -> AsyncIterator[ProviderChunk]:
    """Provider-agnostic stream wrapper: strips markdown emphasis
    markers from text/thinking deltas. Per-channel carry buffers
    handle markers split across deltas. Non-text chunks pass through.

    Sits between the provider and any downstream consumer (inline
    tool-call recovery, validation, UI). Wraps at the agent-runner
    boundary so every provider gets it uniformly."""
    text_carry: list[str] = []
    think_carry: list[str] = []
    async for chunk in inner:
        if chunk.kind == "text" and chunk.text:
            scrubbed = strip_markdown(chunk.text, text_carry)
            if scrubbed:
                yield replace(chunk, text=scrubbed)
            continue
        if chunk.kind == "thinking" and chunk.text:
            scrubbed = strip_markdown(chunk.text, think_carry)
            if scrubbed:
                yield replace(chunk, text=scrubbed)
            continue
        yield chunk
    tail = flush_markdown_carry(text_carry)
    if tail:
        yield ProviderChunk(kind="text", text=tail)
    think_tail = flush_markdown_carry(think_carry)
    if think_tail:
        yield ProviderChunk(kind="thinking", text=think_tail)
