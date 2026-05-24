"""Provider abstraction.

One HTTP round per `stream()` call. The provider knows nothing about
multi-turn assembly or tool execution -- those live in the coordinator
(see `ai-analysis-progress.md` decision log for the rationale).

Mock boundary for tests: `CannedProvider` (text-only, Phase 0) and
`ScriptedProvider` (text + tool_use, Phase 1) -- both in sibling
modules. We do NOT mock at the HTTP layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal


ChunkKind = Literal["text", "thinking", "tool_use"]


@dataclass(slots=True, frozen=True)
class ProviderChunk:
    """One streamed unit from a provider.

    Text / thinking: `text` carries the delta; tool_* fields stay empty.
    Tool use: `text` empty; `tool_use_id`/`tool_name`/`tool_input` describe
    the call the model wants to make. The coordinator dispatches it and
    appends a tool_result message to the next round's input -- tool_results
    flow over the wire as `messages`, not as `ProviderChunk`s, so there is
    no `tool_result` chunk kind on the read side.
    """
    kind: ChunkKind
    text: str = ""
    tool_use_id: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)


Message = dict[str, Any]
# Wire-shape tool schema as sent to the provider. Canonical shape is
# Anthropic's: {name, description, input_schema}. Spec §Providers locks
# this choice -- Ollama's provider translates to OpenAI's function-call
# format ({"type": "function", "function": {name, description, parameters}})
# on the wire. The same translation applies to tool_use blocks in
# responses and tool_result messages on the way back up.
#
# Structured ToolSpec lives in llm/tools.py; ToolRegistry.schemas()
# exports to this canonical (Anthropic) shape.
ToolWireSpec = dict[str, Any]


class LLMProvider(ABC):
    @abstractmethod
    def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        """Run one round and stream its chunks.

        - `system`: system prompt (assembled by the coordinator).
        - `messages`: full accumulated transcript so far (user + assistant
          + tool_result messages). The provider sends this as-is to the
          wire and does not mutate it.
        - `tools`: tool schemas the model may request; None when the
          coordinator runs without tools.

        Subclasses MUST be cancel-safe -- a cancelled task on the
        consumer side must not leak provider state or HTTP connections.
        """
