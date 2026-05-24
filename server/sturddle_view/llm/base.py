"""Provider abstraction.

Streaming + tool-use is the canonical interface, modeled on cluesmith's
`generate_thinking_stream_with_tools`. v1 yields only text chunks; tool
plumbing is added in a later phase but the shape is fixed now so the
agent runner doesn't need a rewrite.

Mock boundary for tests: a `CannedProvider` (sibling module) is the only
double the test suite needs. We do NOT mock at the HTTP layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Literal


ChunkKind = Literal["text", "thinking", "tool_use", "tool_result"]


@dataclass(slots=True, frozen=True)
class ProviderChunk:
    """One streamed unit from a provider.

    For text/thinking, `text` carries the delta. For tool_use / tool_result,
    later phases will populate `tool_use_id`, `tool_name`, `tool_input`,
    `tool_output` -- fields stay None in v1.
    """
    kind: ChunkKind
    text: str = ""


class LLMProvider(ABC):
    @abstractmethod
    def stream(self, system: str, user_msg: str) -> AsyncIterator[ProviderChunk]:
        """Return an async iterator of `ProviderChunk`. Subclasses MUST be
        cancel-safe -- a cancelled task on the consumer side must not leak
        provider state or HTTP connections."""
