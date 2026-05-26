"""Harmony / channel marker stripper.

Buffered scrub so a marker split across streamed deltas is still
removed. Pure functions; state lives in a caller-owned list[str].
"""
from __future__ import annotations

import pytest

from sturddle_view.llm.harmony_strip import (
    flush_harmony_carry,
    strip_harmony_text,
)


def _new_carry() -> list[str]:
    return []


def test_no_marker_passes_through():
    carry = _new_carry()
    assert strip_harmony_text("hello world", carry) == "hello world"
    assert flush_harmony_carry(carry) == ""


def test_complete_marker_in_single_delta_stripped():
    carry = _new_carry()
    assert strip_harmony_text("before<channel|>after", carry) == "beforeafter"
    assert flush_harmony_carry(carry) == ""


def test_canonical_harmony_markers_stripped():
    carry = _new_carry()
    text = "<|start|>assistant<|channel|>analysis<|message|>hi<|end|>"
    assert strip_harmony_text(text, carry) == "assistantanalysishi"
    assert flush_harmony_carry(carry) == ""


def test_marker_split_across_two_deltas_stripped():
    carry = _new_carry()
    first = strip_harmony_text("text<chan", carry)
    second = strip_harmony_text("nel|>more", carry)
    assert first == "text"
    assert second == "more"
    assert flush_harmony_carry(carry) == ""


def test_marker_split_across_three_deltas_stripped():
    carry = _new_carry()
    a = strip_harmony_text("x<", carry)
    b = strip_harmony_text("|chan", carry)
    c = strip_harmony_text("nel|>y", carry)
    assert a + b + c == "xy"
    assert flush_harmony_carry(carry) == ""


def test_partial_at_end_flushed_when_stream_ends():
    # User-supplied prose containing a literal `<|` that never
    # completes into a marker. flush_harmony_carry must surface it
    # rather than swallow.
    carry = _new_carry()
    emit = strip_harmony_text("text <|", carry)
    assert emit == "text "
    assert flush_harmony_carry(carry) == "<|"


def test_consecutive_markers_stripped():
    carry = _new_carry()
    assert strip_harmony_text("<|a|><|b|>", carry) == ""
    assert flush_harmony_carry(carry) == ""


def test_marker_at_start_of_delta():
    carry = _new_carry()
    assert strip_harmony_text("<|start|>hello", carry) == "hello"


def test_marker_at_end_of_delta():
    carry = _new_carry()
    assert strip_harmony_text("hello<|end|>", carry) == "hello"


def test_lone_less_than_held_then_flushed_as_benign():
    carry = _new_carry()
    a = strip_harmony_text("look at <", carry)
    assert a == "look at "
    b = strip_harmony_text(" this", carry)
    # The `<` was held back as a potential marker start; on a delta
    # that doesn't extend it into a marker, it stays held until either
    # a marker completes or the stream ends. The held char surfaces
    # via the next emit-or-flush, NOT silently swallowed.
    flushed = b + flush_harmony_carry(carry)
    assert "<" in flushed


def test_html_like_text_not_eaten():
    # Bare `<foo>` (no `|>` closer) is held mid-stream because it might
    # be the start of a longer marker; flush surfaces it untouched at
    # stream end so the user-visible text is preserved.
    carry = _new_carry()
    emit = strip_harmony_text("<foo>", carry)
    assert emit + flush_harmony_carry(carry) == "<foo>"


@pytest.mark.parametrize("marker", [
    "<|start|>", "<|end|>", "<|channel|>", "<|message|>", "<|return|>",
    "<channel|>", "<|>",
])
def test_known_markers_individually_stripped(marker):
    carry = _new_carry()
    text = f"x{marker}y"
    assert strip_harmony_text(text, carry) == "xy"
    assert flush_harmony_carry(carry) == ""


def test_uppercase_marker_stripped():
    # Case-insensitive regex catches tag variants.
    carry = _new_carry()
    assert strip_harmony_text("a<|END|>b", carry) == "ab"
    assert flush_harmony_carry(carry) == ""


def test_open_marker_followed_by_long_payload_held():
    # An open `<` very early in a long delta must still be detected.
    # The old max_lookback=32 cap could miss this; the forward-scan
    # implementation handles arbitrary deltas.
    carry = _new_carry()
    payload = "x" * 200  # well past the old 32-char window
    emit = strip_harmony_text("<" + payload, carry)
    # The `<...` tail is held; nothing emitted yet because it might be
    # the start of a marker that completes on the next delta.
    assert emit == ""
    completion = strip_harmony_text("|>tail", carry)
    # The held `<` + payload + completing `|>` form one matched marker,
    # so everything between collapses; only "tail" emits.
    assert completion == "tail"
    assert flush_harmony_carry(carry) == ""
