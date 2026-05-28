"""Flavor protocol and result types for inline tool-call recovery.

A Flavor recognizes one inline tool-call shape (XML, fenced JSON,
call-syntax, ...). It is a pure stream-buffer transform: given a
buffer it reports where its sentinel starts, attempts to close a
match, and advises how many trailing characters to hold back so a
sentinel split across chunks still reassembles.

State owns the buffer, channel, and registry; flavors are stateless
beyond what their constructor captures (e.g., compiled tool-name
regex for call-syntax).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..base import ProviderChunk


@dataclass(frozen=True)
class Pending:
    """Sentinel matched but body not yet closed; wait for more chunks."""


@dataclass(frozen=True)
class Closed:
    """Body parsed into a synthetic tool_use chunk; `tail` is any
    prose immediately following the closer."""
    chunk: ProviderChunk
    tail: str


@dataclass(frozen=True)
class NotTool:
    """Sentinel closed but the contents weren't a tool call after all.
    `flushed_text` is the full span (sentinel through closer plus
    trailing tail) to emit verbatim. Currently only fenced-JSON uses
    this -- a ```json fence whose body isn't a `{tool, args}` dict."""
    flushed_text: str


@dataclass(frozen=True)
class Unparseable:
    """Sentinel and closer both present but the body didn't parse.
    `consumed` is how many bytes of buf the State should flush as
    text before retrying. Used by call-syntax where `name(bad-args)`
    closes balanced but args reject -- flush the span as prose and
    keep scanning."""
    consumed: int


CloseResult = Pending | Closed | NotTool | Unparseable


class Flavor(Protocol):
    """Recognizer for one inline tool-call shape."""

    name: str

    def find_sentinel(self, buf: str) -> int | None:
        """Earliest index in `buf` where this flavor's opener starts,
        or None. Called only when not already capturing any flavor."""
        ...

    def try_close(self, buf: str) -> CloseResult:
        """Attempt to close a match. `buf` starts at the sentinel
        (i.e., the caller has already sliced)."""
        ...

    def trailing_hold(self, buf: str) -> int:
        """Number of trailing characters of `buf` to hold back for the
        next chunk because they could complete this flavor's sentinel
        on reassembly. Zero when nothing needs holding."""
        ...
