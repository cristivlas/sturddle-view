"""Fenced-JSON flavor: ```json {"tool": "...", "args": {...}} ```.

Currently a wrapper around the legacy `_try_close_fenced_json` helper.
Step 2 of the refactor will replace this with a native implementation.
"""
from __future__ import annotations

from ..protocol import Closed, CloseResult, NotTool, Pending
from ...inline_tool_calls import (
    _FENCE_OPEN_RE,
    _looks_like_fence_prefix,
    _try_close_fenced_json,
)

_FENCE_TICKS = "```"
_FENCE_LANG = "json"
_FENCE_TAIL_SLACK = 2  # optional whitespace + newline after the language tag
# Upper bound on the trailing prefix that could complete a fence
# opener. Structural -- changing it would break detection.
_FENCE_PREFIX_MAX = len(_FENCE_TICKS) + len(_FENCE_LANG) + _FENCE_TAIL_SLACK


class FencedJsonFlavor:
    name = "fenced_json"

    def find_sentinel(self, buf: str) -> int | None:
        m = _FENCE_OPEN_RE.search(buf)
        return m.start() if m is not None else None

    def try_close(self, buf: str) -> CloseResult:
        closed = _try_close_fenced_json(buf)
        if closed is None:
            return Pending()
        head, payload = closed
        if isinstance(head, str) and head == "nottool":
            return NotTool(flushed_text=payload)
        # Legacy returns (chunk, tail) on a real tool call.
        return Closed(chunk=closed[0], tail=closed[1])

    def trailing_hold(self, buf: str) -> int:
        """Hold back any trailing partial of the fence opener."""
        max_hold = 0
        for i in range(1, min(_FENCE_PREFIX_MAX, len(buf)) + 1):
            tail = buf[-i:]
            if _FENCE_OPEN_RE.match(tail + "\n") or _looks_like_fence_prefix(tail):
                if i > max_hold:
                    max_hold = i
        return max_hold
