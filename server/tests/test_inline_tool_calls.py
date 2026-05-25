"""Inline-XML tool call recovery for prose-style tool emissions.

Some local models stream tool calls as text instead of using the
OpenAI tool_call protocol. The wrapper buffers the XML, parses it,
and emits a synthetic tool_use chunk in its place.
"""
from __future__ import annotations

import pytest

from sturddle_view.llm.base import ProviderChunk
from sturddle_view.llm.inline_tool_calls import recover_inline_tool_calls


async def _from_iter(chunks):
    for c in chunks:
        yield c


async def _collect(it):
    out = []
    async for c in it:
        out.append(c)
    return out


# Real shape captured from nemotron-3-nano:4b on a Sturddle game.
_REAL_XML = (
    "<function=piece_at>\n"
    "<parameter=square>\n"
    "f3\n"
    "</parameter>\n"
    "</function>\n"
    "</tool_call>"
)


@pytest.mark.asyncio
async def test_passthrough_when_no_xml():
    chunks = [
        ProviderChunk(kind="text", text="hello "),
        ProviderChunk(kind="text", text="world"),
    ]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert [c.kind for c in out] == ["text", "text"]
    assert "".join(c.text for c in out) == "hello world"


@pytest.mark.asyncio
async def test_non_text_chunks_pass_through():
    chunks = [
        ProviderChunk(kind="thinking", text="hmm"),
        ProviderChunk(
            kind="tool_use", tool_use_id="t1",
            tool_name="real", tool_input={"k": "v"},
        ),
    ]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert len(out) == 2
    assert out[0].kind == "thinking"
    assert out[1].kind == "tool_use"
    assert out[1].tool_use_id == "t1"  # not rewritten


@pytest.mark.asyncio
async def test_complete_xml_in_single_chunk_recovered():
    chunks = [ProviderChunk(kind="text", text=_REAL_XML)]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert len(out) == 1
    assert out[0].kind == "tool_use"
    assert out[0].tool_name == "piece_at"
    assert out[0].tool_input == {"square": "f3"}
    assert out[0].tool_use_id.startswith("inline-")


@pytest.mark.asyncio
async def test_xml_split_across_chunks_recovered():
    # Real streaming behavior: model emits XML token-by-token.
    pieces = ["<function=", "piece_at>", "\n<parameter=", "square>\nf3\n", "</parameter>\n</function>\n</tool_call>"]
    chunks = [ProviderChunk(kind="text", text=p) for p in pieces]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert len(out) == 1
    assert out[0].kind == "tool_use"
    assert out[0].tool_name == "piece_at"
    assert out[0].tool_input == {"square": "f3"}


@pytest.mark.asyncio
async def test_prose_before_xml_is_emitted():
    chunks = [
        ProviderChunk(kind="text", text="Let me check. "),
        ProviderChunk(kind="text", text=_REAL_XML),
    ]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert len(out) == 2
    assert out[0].kind == "text"
    assert out[0].text == "Let me check. "
    assert out[1].kind == "tool_use"


@pytest.mark.asyncio
async def test_prose_after_xml_is_emitted():
    tail = "Now I'll think."
    chunks = [ProviderChunk(kind="text", text=_REAL_XML + tail)]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert len(out) == 2
    assert out[0].kind == "tool_use"
    assert out[1].kind == "text"
    assert out[1].text == tail


@pytest.mark.asyncio
async def test_multiple_parameters_recovered():
    xml = (
        "<function=analyze>\n"
        "<parameter=fen>\nstartpos\n</parameter>\n"
        "<parameter=depth>\n12\n</parameter>\n"
        "</function>\n</tool_call>"
    )
    chunks = [ProviderChunk(kind="text", text=xml)]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert len(out) == 1
    assert out[0].tool_name == "analyze"
    # Numeric values coerce via json.loads; string values stay strings.
    assert out[0].tool_input == {"fen": "startpos", "depth": 12}


@pytest.mark.asyncio
async def test_param_type_coercion():
    xml = (
        "<function=t>\n"
        "<parameter=s>\nhello\n</parameter>\n"
        "<parameter=i>\n42\n</parameter>\n"
        "<parameter=f>\n3.14\n</parameter>\n"
        "<parameter=b>\ntrue\n</parameter>\n"
        "<parameter=arr>\n[1,2,3]\n</parameter>\n"
        "</function>"
    )
    chunks = [ProviderChunk(kind="text", text=xml)]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert out[0].tool_input == {
        "s": "hello", "i": 42, "f": 3.14, "b": True, "arr": [1, 2, 3],
    }


@pytest.mark.asyncio
async def test_unclosed_xml_flushed_at_stream_end():
    # Model started emitting XML but the stream ended before close;
    # don't silently lose what we buffered.
    chunks = [ProviderChunk(kind="text", text="<function=piece_at>\nincomplete")]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert len(out) == 1
    assert out[0].kind == "text"
    assert out[0].text == "<function=piece_at>\nincomplete"


@pytest.mark.asyncio
async def test_function_close_without_tool_call_trailer():
    # Some models emit </function> but not </tool_call>.
    xml = "<function=piece_at>\n<parameter=square>\ne4\n</parameter>\n</function>"
    chunks = [ProviderChunk(kind="text", text=xml)]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert len(out) == 1
    assert out[0].kind == "tool_use"
    assert out[0].tool_name == "piece_at"
    assert out[0].tool_input == {"square": "e4"}
