"""Provider abstraction.

One HTTP round per `stream()` call. The provider knows nothing about
multi-turn assembly or tool execution -- those live in the coordinator.

The provider boundary is also the test mock boundary: `CannedProvider`
(text-only) and `ScriptedProvider` (text + tool_use) live in sibling
modules and let the coordinator be tested without HTTP. We do NOT mock
at the HTTP layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal

if TYPE_CHECKING:
    from .transcript import Transcript


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
    async def _tx_request(
        self,
        transcript: "Transcript | None",
        round_index: int,
        body: dict[str, Any],
    ) -> None:
        """Capture the request body about to go on the wire.

        Centralized here so every provider's transcript story is the
        same: same event label, same null-handling, same place to extend
        when transcript schema evolves.
        """
        if transcript is not None:
            await transcript.request(round_index, body)

    async def _tx_wire(
        self,
        transcript: "Transcript | None",
        round_index: int,
        line: str,
    ) -> None:
        """Capture one raw line off the wire (parsed or not).

        Called *before* the provider attempts to parse the line, so a
        crash in parsing still leaves the byte trail behind.
        """
        if transcript is not None:
            await transcript.wire_line(round_index, line)

    @abstractmethod
    def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None = None,
        *,
        transcript: "Transcript | None" = None,
        round_index: int = 0,
    ) -> AsyncIterator[ProviderChunk]:
        """Run one round and stream its chunks.

        - `system`: system prompt (assembled by the coordinator).
        - `messages`: full accumulated transcript so far (user + assistant
          + tool_result messages). The provider sends this as-is to the
          wire and does not mutate it.
        - `tools`: tool schemas the model may request; None when the
          coordinator runs without tools.
        - `transcript`: optional sink for the request body and the raw
          wire bytes coming back from the model. When None, the provider
          does no transcript writes. The coordinator passes a
          NullTranscript by default so subclasses never need to null-check.
        - `round_index`: which round of the agent loop this call belongs
          to (0-based). Carried into transcript labels.

        Subclasses MUST be cancel-safe -- a cancelled task on the
        consumer side must not leak provider state or HTTP connections.
        """

    async def list_models(self) -> list[str]:
        """Return the provider's available model ids.

        Default: raises NotImplementedError so unimplemented providers
        fail loud in the Settings UI (the endpoint surfaces it as a
        5xx that the client falls back from to a free-text input).
        Real providers override.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.list_models is not implemented"
        )
