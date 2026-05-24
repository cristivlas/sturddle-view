"""Tool registry: structured tool specs + dispatch lookup.

The coordinator owns the multi-turn agent loop; this module owns the
name -> (schema, callable) mapping it consults each round. Two surfaces:

- `register(spec, fn)`: tool implementations register themselves
  (typically at app construction time).
- `schemas()`: produces the `tools=[...]` payload providers send on the
  wire. Anthropic-flavored shape (`{name, description, input_schema}`);
  Ollama's provider translates to OpenAI on the wire.

Tool callables are async with the shape:
    `async def tool(input: dict, *, cancel_token) -> dict`
The `cancel_token` is the spec's per-call cancellation handle -- present
on day one even though v1 runs tools sequentially, so the parallel-tool
flip later is config, not refactor (spec §Triggers Forward-looking).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .base import ToolWireSpec


class UnknownToolError(KeyError):
    """Raised when the agent loop requests a tool name not in the
    registry. The runner converts this to a structured tool_result so
    the model can recover instead of crashing the turn."""


@dataclass(slots=True, frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


# Forward reference for the cancel token; concrete class lives in
# llm/cancel.py (Step 2). Typed as Any here to avoid a back-import that
# would couple the two modules before the cancel token contract is set.
ToolCallable = Callable[..., Awaitable[dict[str, Any]]]


class ToolRegistry:
    def __init__(self) -> None:
        # Insertion-ordered dict so schemas() output is stable across
        # calls -- prompt cache keys depend on byte-stable tool blocks.
        self._tools: dict[str, tuple[ToolSpec, ToolCallable]] = {}

    def register(self, spec: ToolSpec, fn: ToolCallable) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = (spec, fn)

    def get(self, name: str) -> ToolCallable:
        try:
            return self._tools[name][1]
        except KeyError as e:
            raise UnknownToolError(name) from e

    def spec(self, name: str) -> ToolSpec:
        try:
            return self._tools[name][0]
        except KeyError as e:
            raise UnknownToolError(name) from e

    def schemas(self) -> list[ToolWireSpec]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
            for spec, _ in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools.keys())
