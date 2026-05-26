"""Strip Harmony / channel control tokens leaking into visible text.

OpenAI's Harmony format segments outputs into channels with delimiter
tokens like `<|start|>`, `<|channel|>analysis<|message|>...`, `<|end|>`.
Some local models (gemma4 variants, gpt-oss derivatives) inherit these
markers and Ollama's chat template does not always strip them, so they
arrive in the assistant `content` stream verbatim.

This module exposes `strip_harmony_text(delta, state)` which:
- scrubs complete markers from `delta`,
- carries any trailing partial-match across calls via `state` so a
  marker split across deltas is still stripped on the next chunk.

The state is a plain `list[str]` (a one-element carry buffer); callers
own it for the duration of one stream() so resets happen naturally per
round.
"""
from __future__ import annotations

import re


# Matches a single marker. Two shapes:
#   <|TAG|>     -- canonical Harmony
#   <TAG|>      -- observed in the wild (gemma4 leak)
# Case-insensitive to catch tag variants the model might emit (no
# guarantee Harmony tags are always lowercase on the wire).
_MARKER_RE = re.compile(r"<\|?[a-z_]*\|>", re.IGNORECASE)


def strip_harmony_text(delta: str, carry: list[str]) -> str:
    """Return `delta` with any complete Harmony markers removed.

    `carry` is a one-element list owned by the caller; this function
    reads `carry[0]` (text held back from a previous call because it
    might be the prefix of a marker) and may rewrite it. On entry the
    caller's leftover is prepended; on return `carry[0]` holds whatever
    trailing prefix-of-a-marker we are still uncertain about.
    """
    buf = (carry[0] if carry else "") + delta
    out = _MARKER_RE.sub("", buf)
    # Find the longest suffix of `out` that could be the start of a
    # future marker (`<`, `<|`, `<|c`, ...). Hold it back; flush rest.
    hold = _trailing_marker_prefix(out)
    emit = out[: len(out) - hold] if hold else out
    if carry:
        carry[0] = out[len(out) - hold:] if hold else ""
    else:
        carry.append(out[len(out) - hold:] if hold else "")
    return emit


def flush_harmony_carry(carry: list[str]) -> str:
    """Return whatever the carry buffer still holds, then clear it.

    Called at stream end so a partial marker that never completed does
    not silently swallow user-visible text. If the held text turns out
    to be benign (e.g. literal `<|` in prose), this surfaces it.
    """
    if not carry:
        return ""
    held = carry[0]
    carry[0] = ""
    return held


# Number of trailing characters of `s` that look like they could begin
# a marker. We do not need to know exactly which marker; only that the
# tail is a prefix of *some* possible marker, so we should not emit it
# yet.
def _trailing_marker_prefix(s: str) -> int:
    if not s:
        return 0
    # Scan forward; the leftmost `<` whose suffix has no closing `|>`
    # is the start of the open marker we are still uncertain about.
    for i, ch in enumerate(s):
        if ch == "<" and "|>" not in s[i:]:
            return len(s) - i
    return 0
