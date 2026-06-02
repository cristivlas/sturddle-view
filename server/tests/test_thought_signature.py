"""Gemini thought_signature round-trip through the OpenAI-compat layer.

Gemini 2.5 with thinking returns an encrypted thought_signature on each
tool_call that MUST be echoed back on the next round, or the follow-up
request 400s ("Function call is missing a thought_signature"). The
signature rides the canonical (Anthropic-shaped) tool_use block under a
provider-neutral key; openai_compat maps it to/from Gemini's
extra_content.google.thought_signature only at the wire.

These are pure-function tests: capture (wire -> chunk), echo (block ->
wire), and inertness when no signature is present (Anthropic/Ollama).
"""
from __future__ import annotations

from sturddle_view.llm import TOOL_SIGNATURE_KEY
from sturddle_view.llm.openai_compat import (
    messages_anthropic_to_openai,
    openai_tool_call_to_provider_chunk,
)


_SIG = "ENCRYPTED_THOUGHT_SIG_abc123"


def _gemini_tool_call(signature: str | None) -> dict:
    tc = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "recommend_move", "arguments": '{"move": "e4"}'},
    }
    if signature is not None:
        tc["extra_content"] = {"google": {"thought_signature": signature}}
    return tc


def test_capture_signature_from_tool_call():
    chunk = openai_tool_call_to_provider_chunk(_gemini_tool_call(_SIG))
    assert chunk.tool_signature == _SIG
    assert chunk.tool_name == "recommend_move"
    assert chunk.tool_input == {"move": "e4"}


def test_capture_empty_when_no_signature():
    chunk = openai_tool_call_to_provider_chunk(_gemini_tool_call(None))
    assert chunk.tool_signature == ""


def test_echo_signature_back_on_tool_use_block():
    msgs = [{
        "role": "assistant",
        "content": [{
            "type": "tool_use",
            "id": "call_1",
            "name": "recommend_move",
            "input": {"move": "e4"},
            TOOL_SIGNATURE_KEY: _SIG,
        }],
    }]
    tc = messages_anthropic_to_openai(msgs)[0]["tool_calls"][0]
    assert tc["extra_content"]["google"]["thought_signature"] == _SIG
    assert tc["id"] == "call_1"


def test_no_extra_content_when_block_has_no_signature():
    # Anthropic / Ollama tool_use blocks carry no signature; the echo
    # path must not invent an extra_content field for them.
    msgs = [{
        "role": "assistant",
        "content": [{
            "type": "tool_use",
            "id": "call_2",
            "name": "analyze",
            "input": {},
        }],
    }]
    tc = messages_anthropic_to_openai(msgs)[0]["tool_calls"][0]
    assert "extra_content" not in tc


def test_round_trip_capture_then_echo():
    # Wire tool_call -> chunk -> canonical block -> wire tool_call.
    chunk = openai_tool_call_to_provider_chunk(_gemini_tool_call(_SIG))
    block = {
        "type": "tool_use",
        "id": chunk.tool_use_id,
        "name": chunk.tool_name,
        "input": chunk.tool_input,
    }
    if chunk.tool_signature:
        block[TOOL_SIGNATURE_KEY] = chunk.tool_signature
    msgs = [{"role": "assistant", "content": [block]}]
    tc = messages_anthropic_to_openai(msgs)[0]["tool_calls"][0]
    assert tc["extra_content"]["google"]["thought_signature"] == _SIG
