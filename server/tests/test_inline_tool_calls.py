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


# Real shape captured from a small local model on a Sturddle game.
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


# ---------- Call-syntax recovery (registry-aware) ---------------------
# Some models stream tool calls as Python/JSON-shaped text instead of
# the wire format. When the caller passes a `tool_names` set, the
# wrapper recognizes any of those names followed by a `(` or `{` and
# tries permissive arg parsing for several shapes.


_NAMES = {"recommend_move", "piece_at", "analyze"}


@pytest.mark.asyncio
async def test_call_python_dict_bare_keys():
    chunks = [ProviderChunk(kind="text", text='recommend_move{move: "e5"}')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_name == "recommend_move"
    assert tool[0].tool_input == {"move": "e5"}


@pytest.mark.asyncio
async def test_call_json_arg():
    chunks = [ProviderChunk(kind="text", text='recommend_move({"move": "e5"})')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": "e5"}


@pytest.mark.asyncio
async def test_call_python_dict_single_quotes():
    chunks = [ProviderChunk(kind="text", text="recommend_move({'move': 'e5'})")]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": "e5"}


@pytest.mark.asyncio
async def test_call_kwarg_quoted():
    chunks = [ProviderChunk(kind="text", text='recommend_move(move="e5")')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": "e5"}


@pytest.mark.asyncio
async def test_call_kwarg_unquoted():
    chunks = [ProviderChunk(kind="text", text="recommend_move(move=e5)")]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": "e5"}


@pytest.mark.asyncio
async def test_call_positional_arg_falls_through_as_text():
    # No positional-arg dispatch -- our tools all take named params.
    # An unrecognized shape stays as text so the model can see and retry.
    chunks = [ProviderChunk(kind="text", text='recommend_move("e5")')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    assert all(c.kind == "text" for c in out)
    assert "".join(c.text for c in out) == 'recommend_move("e5")'


@pytest.mark.asyncio
async def test_call_bool_and_null_literals_coerced():
    # True/False/None must come through as real bools/None, not strings.
    chunks = [ProviderChunk(kind="text", text='recommend_move(move=e5, deep=True, alt=None)')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": "e5", "deep": True, "alt": None}


@pytest.mark.asyncio
async def test_call_escaped_quote_in_string_arg():
    # Backslash-escaped quote must not close the string prematurely.
    chunks = [ProviderChunk(kind="text", text='recommend_move(move="e5\\"x")')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": 'e5"x'}


@pytest.mark.asyncio
async def test_call_split_across_chunks():
    chunks = [
        ProviderChunk(kind="text", text="recommend_"),
        ProviderChunk(kind="text", text='move{move: "e5"}'),
    ]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": "e5"}


@pytest.mark.asyncio
async def test_unregistered_name_passes_through():
    chunks = [ProviderChunk(kind="text", text='bogus_name(move="e5")')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    assert all(c.kind == "text" for c in out)
    assert "".join(c.text for c in out) == 'bogus_name(move="e5")'


@pytest.mark.asyncio
async def test_tool_name_in_prose_without_brackets_not_recovered():
    # The model talks about the tool in prose, no call shape.
    chunks = [ProviderChunk(kind="text", text="The recommend_move tool would say more.")]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    assert all(c.kind == "text" for c in out)


@pytest.mark.asyncio
async def test_prefix_text_preserved():
    chunks = [ProviderChunk(kind="text", text='Pick: recommend_move{move: "e5"}')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    assert out[0].kind == "text"
    assert out[0].text == "Pick: "
    assert out[1].kind == "tool_use"


@pytest.mark.asyncio
async def test_tail_text_preserved():
    chunks = [ProviderChunk(kind="text", text='recommend_move{move: "e5"} done.')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool_idx = next(i for i, c in enumerate(out) if c.kind == "tool_use")
    assert tool_idx + 1 < len(out)
    assert out[tool_idx + 1].kind == "text"
    assert out[tool_idx + 1].text == " done."


@pytest.mark.asyncio
async def test_malformed_args_flushed_as_text():
    # Open bracket never closes; buffered text must surface at stream end.
    chunks = [ProviderChunk(kind="text", text="recommend_move(move=")]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    assert all(c.kind == "text" for c in out)
    assert "".join(c.text for c in out) == "recommend_move(move="


@pytest.mark.asyncio
async def test_real_wire_tool_use_passes_through():
    chunks = [
        ProviderChunk(kind="tool_use", tool_use_id="t1",
                      tool_name="recommend_move", tool_input={"move": "e5"}),
    ]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    assert len(out) == 1
    assert out[0].kind == "tool_use"
    assert out[0].tool_use_id == "t1"


@pytest.mark.asyncio
async def test_no_tool_names_disables_call_recovery():
    # Back-compat: legacy XML path still works; new patterns ignored.
    chunks = [ProviderChunk(kind="text", text='recommend_move{move: "e5"}')]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks)))
    assert all(c.kind == "text" for c in out)


@pytest.mark.asyncio
async def test_xml_still_works_when_tool_names_provided():
    # Legacy XML recovery is not gated on tool_names.
    chunks = [ProviderChunk(kind="text", text=_REAL_XML)]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_name == "piece_at"
    assert tool[0].tool_input == {"square": "f3"}


# ---------- "call:" prefix + bare-value shape -------------------------
# Real shape captured live in a model's thinking stream:
#   call:analyze{fen:5r2/2qbbppk/...,depth:28,time_ms:150}
# Three new wrinkles: a `call:` decoration before the tool name,
# unquoted (bare) string values, and unquoted bare-identifier keys.


@pytest.mark.asyncio
async def test_call_prefix_with_bare_values():
    fen = "5r2/2qbbppk/rpn4n/2p2p1p/P1PpP3/3P1NPP/3BN1BK/R2Q1R2 w - - 0 20"
    chunks = [ProviderChunk(
        kind="text",
        text=f"call:analyze{{fen:{fen},depth:28,time_ms:150}}",
    )]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_name == "analyze"
    assert tool[0].tool_input == {"fen": fen, "depth": 28, "time_ms": 150}


@pytest.mark.asyncio
async def test_call_prefix_with_paren_args():
    chunks = [ProviderChunk(
        kind="text", text="call:piece_at(square:e4)",
    )]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"square": "e4"}


@pytest.mark.asyncio
async def test_call_prefix_then_text_preserved():
    # Tail text after the recovered call survives.
    chunks = [ProviderChunk(
        kind="text", text='call:piece_at{square:e4} then more prose.',
    )]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool_idx = next(i for i, c in enumerate(out) if c.kind == "tool_use")
    assert tool_idx + 1 < len(out)
    assert out[tool_idx + 1].kind == "text"
    assert out[tool_idx + 1].text == " then more prose."


@pytest.mark.asyncio
async def test_call_prefix_split_at_colon_boundary():
    # Regression: live stream emitted `call:` and `recommend_move{...}`
    # in separate chunks. The colon must not flush early before the
    # second chunk can complete the match.
    chunks = [
        ProviderChunk(kind="text", text="call:"),
        ProviderChunk(kind="text", text="recommend_move{depth:20,move:Be3}"),
    ]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_name == "recommend_move"
    assert tool[0].tool_input == {"depth": 20, "move": "Be3"}


@pytest.mark.asyncio
async def test_call_prefix_split_mid_prefix():
    # Worse split: prefix broken at "ca" / "ll:" / "recommend_move...".
    chunks = [
        ProviderChunk(kind="text", text="ca"),
        ProviderChunk(kind="text", text="ll:"),
        ProviderChunk(kind="text", text="recommend_move{move:Be3}"),
    ]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": "Be3"}


@pytest.mark.asyncio
async def test_call_prefix_char_by_char_does_not_leak_prefix():
    # Regression: live token-by-token stream leaked the literal `call:`
    # to the user before recovery completed. Buffer must hold back any
    # partial prefix that could complete into a recoverable shape.
    text = "call:recommend_move{move:Be3}"
    chunks = [ProviderChunk(kind="text", text=c) for c in text]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    text_emitted = "".join(c.text for c in out if c.kind == "text")
    assert text_emitted == "", f"expected no text leak, got {text_emitted!r}"
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": "Be3"}


# ---------- thinking-channel inline call recovery ---------------------
# Regression: models that stream reasoning into `thinking` sometimes
# emit a real tool call there too, never on the text channel. Recovery
# must also process thinking chunks; non-call portions stay as
# `thinking`, recovered calls become `tool_use`.


@pytest.mark.asyncio
async def test_inline_call_in_thinking_recovered():
    chunks = [ProviderChunk(
        kind="thinking",
        text="I should look at moves. call:recommend_move{move:Be3}",
    )]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_name == "recommend_move"
    assert tool[0].tool_input == {"move": "Be3"}
    # Prefix prose preserved as thinking, not promoted to text.
    thinking_text = "".join(c.text for c in out if c.kind == "thinking")
    assert "I should look at moves." in thinking_text
    text_emitted = "".join(c.text for c in out if c.kind == "text")
    assert text_emitted == ""


@pytest.mark.asyncio
async def test_inline_xml_call_in_thinking_recovered():
    chunks = [ProviderChunk(kind="thinking", text=_REAL_XML)]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_name == "piece_at"
    assert tool[0].tool_input == {"square": "f3"}


@pytest.mark.asyncio
async def test_thinking_without_call_passes_through_as_thinking():
    chunks = [ProviderChunk(kind="thinking", text="just musing about Be3 and Nf3")]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    assert all(c.kind == "thinking" for c in out)
    assert "".join(c.text for c in out) == "just musing about Be3 and Nf3"
    assert not any(c.kind == "tool_use" for c in out)


@pytest.mark.asyncio
async def test_inline_call_split_across_thinking_chunks_recovered():
    text = "call:recommend_move{move:Be3}"
    chunks = [ProviderChunk(kind="thinking", text=c) for c in text]
    out = await _collect(recover_inline_tool_calls(_from_iter(chunks), tool_names=_NAMES))
    tool = [c for c in out if c.kind == "tool_use"]
    assert len(tool) == 1
    assert tool[0].tool_input == {"move": "Be3"}
    text_emitted = "".join(c.text for c in out if c.kind == "text")
    assert text_emitted == ""
