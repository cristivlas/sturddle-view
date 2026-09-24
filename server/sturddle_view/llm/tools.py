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
flip later is config, not refactor (spec sec. Triggers Forward-looking).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .base import ToolWireSpec

_TYPE_KEY = "type"
_DESCRIPTION_KEY = "description"
_STRING_TYPE = "string"


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """A tool's JSON Schema input: an object with these properties."""
    return {_TYPE_KEY: "object", "properties": properties, "required": required}


def string_prop(description: str) -> dict[str, Any]:
    return {_TYPE_KEY: _STRING_TYPE, _DESCRIPTION_KEY: description}


def integer_prop(description: str) -> dict[str, Any]:
    return {_TYPE_KEY: "integer", _DESCRIPTION_KEY: description}


def string_list_prop(description: str) -> dict[str, Any]:
    return {
        _TYPE_KEY: "array",
        "items": {_TYPE_KEY: _STRING_TYPE},
        _DESCRIPTION_KEY: description,
    }


class UnknownToolError(KeyError):
    """Raised when the agent loop requests a tool name not in the
    registry. The runner converts this to a structured tool_result so
    the model can recover instead of crashing the turn."""


@dataclass(slots=True, frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Post-call usage guidance, lazy-loaded on first tool call per turn.
    # See docs/ai-analysis-spec.md sec. Skills layer. None = no card injected.
    card: str | None = None


# Forward reference for the cancel token; concrete class lives in
# llm/cancel.py. Typed as Any here to avoid a back-import that would
# couple the two modules.
ToolCallable = Callable[..., Awaitable[dict[str, Any]]]


class ToolRegistry:
    def __init__(self) -> None:
        # Insertion-ordered dict so schemas() output is stable across
        # calls -- the tool block the model sees doesn't reshuffle.
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
                _DESCRIPTION_KEY: spec.description,
                "input_schema": spec.input_schema,
            }
            for spec, _ in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def specs(self) -> list[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]
