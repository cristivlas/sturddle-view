"""Flavor-registry implementation of inline tool-call recovery.

Some local models stream tool calls as prose instead of using the
OpenAI tool_call protocol. Each flavor (XML, fenced-JSON, call-syntax,
etc.) owns its own sentinel detection and close logic; the State in
`.state` walks the registry. Shared parsing utilities live in
`..inline_tool_calls`.
"""
from __future__ import annotations

from typing import AsyncIterator, Iterable, Mapping, Sequence

from ..base import ProviderChunk
from .state import recover as _recover


ToolSchemas = Mapping[str, Sequence[str]]


def recover_inline_tool_calls(
    upstream: AsyncIterator[ProviderChunk],
    *,
    tool_names: Iterable[str] | None = None,
    tool_schemas: ToolSchemas | None = None,
) -> AsyncIterator[ProviderChunk]:
    return _recover(upstream, tool_names=tool_names, tool_schemas=tool_schemas)


__all__ = ["recover_inline_tool_calls"]
