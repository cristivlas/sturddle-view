"""Slice B Step 1: ToolRegistry contract.

Registry is the lookup layer between the agent runner and tool
implementations. It owns:
- name -> (schema, callable) mapping
- schema export in the provider's tools=[...] shape (Anthropic-flavored;
  Ollama provider translates to OpenAI on the wire)
- structured error for unknown-name dispatch (no exception leak)
"""
from __future__ import annotations

import pytest

from sturddle_view.llm.tools import ToolRegistry, ToolSpec, UnknownToolError


async def _noop(_input, *, cancel_token):
    return {"ok": True}


def test_register_and_lookup_by_name():
    reg = ToolRegistry()
    spec = ToolSpec(
        name="analyze",
        description="run a search",
        input_schema={"type": "object", "properties": {"fen": {"type": "string"}}},
    )
    reg.register(spec, _noop)
    assert reg.get("analyze") is _noop
    assert reg.spec("analyze") is spec


def test_schemas_exports_provider_tools_shape():
    reg = ToolRegistry()
    spec_a = ToolSpec(name="a", description="A", input_schema={"type": "object"})
    spec_b = ToolSpec(name="b", description="B", input_schema={"type": "object"})
    reg.register(spec_a, _noop)
    reg.register(spec_b, _noop)
    out = reg.schemas()
    # Anthropic shape: list of {name, description, input_schema}. Stable
    # order across calls so prompt-cache keys don't shift round to round.
    assert out == [
        {"name": "a", "description": "A", "input_schema": {"type": "object"}},
        {"name": "b", "description": "B", "input_schema": {"type": "object"}},
    ]


def test_unknown_name_raises_typed_error():
    reg = ToolRegistry()
    with pytest.raises(UnknownToolError) as ei:
        reg.get("missing")
    assert "missing" in str(ei.value)


def test_duplicate_registration_rejected():
    reg = ToolRegistry()
    spec = ToolSpec(name="dup", description="", input_schema={})
    reg.register(spec, _noop)
    with pytest.raises(ValueError, match="dup"):
        reg.register(spec, _noop)
