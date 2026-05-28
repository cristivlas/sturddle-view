"""Unit tests for the Ollama provider's wire translations.

Canonical internal shape is Anthropic (spec §Providers); the Ollama
provider translates to/from OpenAI on the wire. These tests pin both
directions in isolation -- pure-function level, no HTTP -- so wire
bugs don't masquerade as agent-loop bugs.
"""
from __future__ import annotations

import json

import pytest

from sturddle_view.llm.ollama import (
    messages_anthropic_to_openai,
    tools_anthropic_to_openai,
    openai_tool_call_to_provider_chunk,
)


def test_user_text_message_passes_through():
    out = messages_anthropic_to_openai([
        {"role": "user", "content": "hello"},
    ])
    assert out == [{"role": "user", "content": "hello"}]


def test_assistant_message_with_text_and_tool_use_becomes_assistant_with_tool_calls():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me check. "},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "analyze",
                "input": {"fen": "startpos"},
            },
        ],
    }
    out = messages_anthropic_to_openai([msg])
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "Let me check. "
    calls = out[0]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["id"] == "tu_1"
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "analyze"
    # Arguments must be JSON-encoded per the OpenAI spec.
    assert json.loads(calls[0]["function"]["arguments"]) == {"fen": "startpos"}


def test_tool_result_message_becomes_tool_role_message():
    msg = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": '{"score_cp": 42}',
            },
        ],
    }
    out = messages_anthropic_to_openai([msg])
    assert out == [{
        "role": "tool",
        "tool_call_id": "tu_1",
        "content": '{"score_cp": 42}',
    }]


def test_multiple_tool_results_split_into_multiple_tool_messages():
    # Anthropic packs multiple tool_results into one user message;
    # OpenAI wants one per tool message.
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "1"},
            {"type": "tool_result", "tool_use_id": "b", "content": "2"},
        ],
    }
    out = messages_anthropic_to_openai([msg])
    assert out == [
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"},
    ]


def test_tools_schema_translation():
    tools = [
        {
            "name": "analyze",
            "description": "search a position",
            "input_schema": {"type": "object", "properties": {"fen": {"type": "string"}}},
        },
    ]
    out = tools_anthropic_to_openai(tools)
    assert out == [{
        "type": "function",
        "function": {
            "name": "analyze",
            "description": "search a position",
            "parameters": {
                "type": "object",
                "properties": {"fen": {"type": "string"}},
            },
        },
    }]


def test_openai_tool_call_to_provider_chunk():
    # Accumulated tool_call from streaming deltas.
    tc = {
        "id": "tu_x",
        "type": "function",
        "function": {
            "name": "analyze",
            "arguments": '{"fen": "rnbq"}',
        },
    }
    chunk = openai_tool_call_to_provider_chunk(tc)
    assert chunk.kind == "tool_use"
    assert chunk.tool_use_id == "tu_x"
    assert chunk.tool_name == "analyze"
    assert chunk.tool_input == {"fen": "rnbq"}


def test_openai_tool_call_with_empty_arguments_yields_empty_input():
    # Some models stream the function name but no arguments string for
    # zero-argument tools; we must not blow up on json.loads("").
    tc = {
        "id": "tu_y",
        "type": "function",
        "function": {"name": "ping", "arguments": ""},
    }
    chunk = openai_tool_call_to_provider_chunk(tc)
    assert chunk.tool_input == {}
