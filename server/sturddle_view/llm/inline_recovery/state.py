"""Stream-level state machine for inline tool-call recovery.

Walks a flavor registry. Per chunk:
  1. Non-recoverable chunk (non-text/thinking, or empty text) -> flush
     pending text-buf, pass through.
  2. Channel switch (text<->thinking) with pending state -> flush all
     pending buffers as the prior channel, then continue.
  3. If currently capturing one flavor's body, feed the chunk into that
     capture buf and call its try_close; emit on Closed / NotTool /
     Unparseable, stay on Pending.
  4. Else scan flavors in priority order for the earliest sentinel; on
     match, slice prefix as text and enter capturing mode on that
     flavor. If try_close immediately succeeds, emit and continue.
  5. Else no flavor matched -- compute the union trailing-hold across
     all flavors; flush the safe prefix, hold the rest in text_buf.

Stream end: flush any remaining buffers as text on the current channel.

This replaces the flat state machine in `..inline_tool_calls` whose
parallel buffers (xml_buf, fence_buf, call_buf, pre_buf) and capture
flags became the documented refactor smell.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, List, Optional

from ..base import ProviderChunk
from .protocol import Closed, Flavor, NotTool, Pending, Unparseable
from .flavors import CallSyntaxFlavor, FencedJsonFlavor, JinjaPipeFlavor, XmlFlavor

log = logging.getLogger(__name__)

_CHANNEL_TEXT = "text"
_CHANNEL_THINKING = "thinking"
# Channels carrying recoverable prose. Other ProviderChunk kinds
# (tool_use, tool_result, etc.) pass through untouched.
_RECOVERABLE_CHANNELS = (_CHANNEL_TEXT, _CHANNEL_THINKING)


def _build_registry(tool_names: Optional[Iterable[str]]) -> List[Flavor]:
    """Priority order: XML, fenced JSON, jinja-pipe, then call-syntax.
    XML and fenced JSON match without `tool_names`; jinja-pipe and
    call-syntax require it."""
    registry: List[Flavor] = [XmlFlavor(), FencedJsonFlavor()]
    if tool_names:
        names = tuple(tool_names)
        jinja = JinjaPipeFlavor(names)
        if jinja.enabled:
            registry.append(jinja)
        call = CallSyntaxFlavor(names)
        if call.enabled:
            registry.append(call)
    return registry


async def recover(
    upstream: AsyncIterator[ProviderChunk],
    *,
    tool_names: Optional[Iterable[str]] = None,
) -> AsyncIterator[ProviderChunk]:
    """Async-iterator wrapper. See module docstring."""
    flavors = _build_registry(tool_names)

    # Mode A (scanning): text_buf accumulates prose that may still
    # contain a sentinel split across chunks.
    # Mode B (capturing): capture_flavor != None and capture_buf holds
    # the in-progress match.
    text_buf = ""
    capture_flavor: Optional[Flavor] = None
    capture_buf = ""
    channel = _CHANNEL_TEXT

    async for chunk in upstream:
        recoverable = chunk.kind in _RECOVERABLE_CHANNELS and chunk.text
        if not recoverable:
            if text_buf:
                yield ProviderChunk(kind=channel, text=text_buf)
                text_buf = ""
            if capture_flavor is not None and capture_buf:
                yield ProviderChunk(kind=channel, text=capture_buf)
                capture_buf = ""
                capture_flavor = None
            yield chunk
            continue

        if chunk.kind != channel and (text_buf or capture_flavor is not None):
            if text_buf:
                yield ProviderChunk(kind=channel, text=text_buf)
                text_buf = ""
            if capture_flavor is not None and capture_buf:
                yield ProviderChunk(kind=channel, text=capture_buf)
                capture_buf = ""
                capture_flavor = None
        channel = chunk.kind

        # Capturing: feed the chunk into the active capture buf, try to close.
        if capture_flavor is not None:
            capture_buf += chunk.text
            drained = list(_drain_capture(capture_flavor, capture_buf, channel, flavors))
            if not drained:
                # Pending: capture_buf stays as-is; wait for more.
                continue
            for out in drained:
                if isinstance(out, _ReenterScan):
                    capture_flavor = None
                    capture_buf = ""
                    # carry is fed back into scanning as if it had just arrived.
                    if out.carry:
                        for inner in _scan_and_capture(out.carry, flavors, channel):
                            if isinstance(inner, _EnterCapture):
                                capture_flavor = inner.flavor
                                capture_buf = inner.buf
                            elif isinstance(inner, _Carry):
                                text_buf = inner.buf
                            else:
                                yield inner
                    continue
                yield out
            continue

        # Scanning: combine pending text_buf with new chunk, then scan.
        combined = text_buf + chunk.text
        text_buf = ""
        scan_result = _scan_and_capture(combined, flavors, channel)
        for out in scan_result:
            if isinstance(out, _EnterCapture):
                capture_flavor = out.flavor
                capture_buf = out.buf
                continue
            if isinstance(out, _Carry):
                text_buf = out.buf
                continue
            yield out

    # Stream end: flush any remaining buffers.
    if capture_flavor is not None and capture_buf:
        log.warning(
            "inline-%s tool call did not close before stream end; flushing %d bytes",
            capture_flavor.name, len(capture_buf),
        )
        yield ProviderChunk(kind=channel, text=capture_buf)
    if text_buf:
        yield ProviderChunk(kind=channel, text=text_buf)


# ---------- internal step results -------------------------------------


@dataclass(frozen=True)
class _ReenterScan:
    """Capture finished; `carry` is text to feed back into scanning
    (typically the tail after a closer)."""
    carry: str


@dataclass(frozen=True)
class _EnterCapture:
    flavor: Flavor
    buf: str


@dataclass(frozen=True)
class _Carry:
    """Trailing hold-back for the next chunk."""
    buf: str


def _drain_capture(flavor: Flavor, buf: str, channel: str, flavors: List[Flavor]):
    """Yield ProviderChunk(s) plus a terminal _ReenterScan when the
    capture closed. On Pending yields nothing (caller keeps capture_buf
    intact and waits for more chunks)."""
    result = flavor.try_close(buf)
    if isinstance(result, Pending):
        return
    if isinstance(result, Closed):
        yield result.chunk
        yield _ReenterScan(carry=result.tail)
        return
    if isinstance(result, NotTool):
        yield ProviderChunk(kind=channel, text=result.flushed_text)
        yield _ReenterScan(carry="")
        return
    if isinstance(result, Unparseable):
        yield ProviderChunk(kind=channel, text=buf[:result.consumed])
        yield _ReenterScan(carry=buf[result.consumed:])
        return
    raise AssertionError(f"unexpected CloseResult: {result!r}")


def _scan_and_capture(buf: str, flavors: List[Flavor], channel: str):
    """Scan `buf` for any flavor's sentinel; on hit, flush prefix as
    text, then attempt close. Loops to handle multiple matches in a
    single buffer. Yields ProviderChunk, _EnterCapture, or _Carry."""
    remaining = buf
    while remaining:
        # Find the earliest sentinel across all flavors, breaking ties
        # by flavor priority (registry order).
        best_idx: Optional[int] = None
        best_flavor: Optional[Flavor] = None
        for fl in flavors:
            idx = fl.find_sentinel(remaining)
            if idx is None:
                continue
            if best_idx is None or idx < best_idx:
                best_idx = idx
                best_flavor = fl
        if best_flavor is None:
            # No sentinel found. Hold any trailing prefix that could
            # complete into a sentinel on the next chunk.
            hold = _max_trailing_hold(remaining, flavors)
            if hold > 0:
                flush = remaining[:-hold]
                carry = remaining[-hold:]
            else:
                flush = remaining
                carry = ""
            if flush:
                yield ProviderChunk(kind=channel, text=flush)
            yield _Carry(buf=carry)
            return
        # Flush prefix prose, then try to close the match.
        if best_idx > 0:
            yield ProviderChunk(kind=channel, text=remaining[:best_idx])
        match_buf = remaining[best_idx:]
        result = best_flavor.try_close(match_buf)
        if isinstance(result, Pending):
            yield _EnterCapture(flavor=best_flavor, buf=match_buf)
            return
        if isinstance(result, Closed):
            yield result.chunk
            remaining = result.tail
            continue
        if isinstance(result, NotTool):
            yield ProviderChunk(kind=channel, text=result.flushed_text)
            remaining = ""  # NotTool consumes the whole span (incl. tail).
            return
        if isinstance(result, Unparseable):
            yield ProviderChunk(kind=channel, text=match_buf[:result.consumed])
            remaining = match_buf[result.consumed:]
            continue
        raise AssertionError(f"unexpected CloseResult: {result!r}")


def _max_trailing_hold(buf: str, flavors: List[Flavor]) -> int:
    """Union trailing-hold across all flavors. Replaces the
    cross-flavor coupling in legacy `_split_at_safe_boundary` /
    `_split_at_sentinel_prefix`."""
    return max((fl.trailing_hold(buf) for fl in flavors), default=0)
