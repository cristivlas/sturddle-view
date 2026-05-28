"""Unit + streaming tests for the markdown strip layer.

Covers the per-delta strip function and the async stream wrapper:
- paired markers (**, __, `) removed in single-chunk and split cases
- single * / _ held back when at end of chunk (could complete to ** / __)
- non-text chunks pass through unchanged
- thinking channel handled with its own carry
"""
from __future__ import annotations

import pytest

from sturddle_view.llm.base import ProviderChunk
from sturddle_view.llm.markdown_strip import (
    flush_markdown_carry,
    strip_markdown,
    strip_markdown_stream,
)


async def _from_iter(chunks):
    for c in chunks:
        yield c


async def _collect(it):
    out = []
    async for c in it:
        out.append(c)
    return out


def test_strip_paired_double_star():
    carry: list[str] = []
    out = strip_markdown("bishop on **f2** now", carry)
    assert out == "bishop on f2 now"
    assert carry == [""]


def test_strip_paired_double_underscore():
    carry: list[str] = []
    out = strip_markdown("__Qxh7+__ wins", carry)
    assert out == "Qxh7+ wins"


def test_strip_backtick():
    carry: list[str] = []
    out = strip_markdown("`Nf3` is fine", carry)
    assert out == "Nf3 is fine"


def test_trailing_single_star_held_back():
    carry: list[str] = []
    out = strip_markdown("the move is *", carry)
    assert out == "the move is "
    assert carry == ["*"]


def test_split_double_star_across_two_deltas():
    carry: list[str] = []
    out1 = strip_markdown("the move is *", carry)
    assert out1 == "the move is "
    out2 = strip_markdown("*f2** good", carry)
    # Reassembly: carry=`*` + delta=`*f2** good` -> `**f2** good` -> `f2 good`
    assert out2 == "f2 good"


def test_lone_star_flushed_at_stream_end():
    carry: list[str] = []
    out = strip_markdown("done *", carry)
    assert out == "done "
    held = flush_markdown_carry(carry)
    assert held == "*"
    assert carry == [""]


@pytest.mark.asyncio
async def test_stream_wrapper_strips_text_chunks():
    chunks = [
        ProviderChunk(kind="text", text="bishop on **"),
        ProviderChunk(kind="text", text="f2** now"),
    ]
    out = await _collect(strip_markdown_stream(_from_iter(chunks)))
    text = "".join(c.text for c in out if c.kind == "text")
    assert text == "bishop on f2 now"


@pytest.mark.asyncio
async def test_stream_wrapper_passes_non_text_chunks():
    chunks = [
        ProviderChunk(kind="text", text="play **f2**"),
        ProviderChunk(
            kind="tool_use",
            tool_use_id="abc",
            tool_name="analyze",
            tool_input={"fen": "..."},
        ),
        ProviderChunk(kind="text", text=" then"),
    ]
    out = await _collect(strip_markdown_stream(_from_iter(chunks)))
    text = "".join(c.text for c in out if c.kind == "text")
    assert text == "play f2 then"
    tool_uses = [c for c in out if c.kind == "tool_use"]
    assert len(tool_uses) == 1
    assert tool_uses[0].tool_name == "analyze"


@pytest.mark.asyncio
async def test_stream_wrapper_handles_thinking_channel_independently():
    chunks = [
        ProviderChunk(kind="thinking", text="__plan__: "),
        ProviderChunk(kind="text", text="**move** "),
        ProviderChunk(kind="thinking", text="continue"),
    ]
    out = await _collect(strip_markdown_stream(_from_iter(chunks)))
    text_combined = "".join(c.text for c in out if c.kind == "text")
    think_combined = "".join(c.text for c in out if c.kind == "thinking")
    assert text_combined == "move "
    assert think_combined == "plan: continue"


@pytest.mark.asyncio
async def test_stream_wrapper_flushes_held_marker_at_end():
    chunks = [ProviderChunk(kind="text", text="done *")]
    out = await _collect(strip_markdown_stream(_from_iter(chunks)))
    text = "".join(c.text for c in out if c.kind == "text")
    # Lone `*` flushed at stream end as user-visible text.
    assert text == "done *"
