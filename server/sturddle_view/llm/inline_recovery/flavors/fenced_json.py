"""Fenced-JSON flavor: ```json {"tool": "...", "args": {...}} ```.

Body keys accepted (in order): name = tool/name/action/function;
args = args/arguments. If neither args key is present, the top-level
dict (minus the name key) is taken as args -- some models drop the
wrapper entirely.

On parse failure or non-tool-shape body, the whole fence (including
the closing backticks) is flushed verbatim as text.
"""
from __future__ import annotations

import json
import logging
import re

from ..protocol import Closed, CloseResult, NotTool, Pending
from ...inline_tool_calls import _synthesize_tool_use

log = logging.getLogger(__name__)

_FENCE_TICKS = "```"
_FENCE_LANG = "json"
_FENCE_TAIL_SLACK = 2  # optional whitespace + newline after the language tag
# Permissive opener: 3 backticks, optional language tag, newline.
# Casing/whitespace variants pass.
_FENCE_OPEN_RE = re.compile(r"```[ \t]*[A-Za-z]*[ \t]*\n")
# Upper bound on the trailing prefix that could complete a fence
# opener. Structural -- changing it would break detection.
_FENCE_PREFIX_MAX = len(_FENCE_TICKS) + len(_FENCE_LANG) + _FENCE_TAIL_SLACK

_NAME_KEYS = ("tool", "name", "action", "function")
_ARGS_KEYS = ("args", "arguments")


def _looks_like_fence_prefix(tail: str) -> bool:
    """True iff `tail` could complete into a fence opener on the next
    chunk. Held back so a split opener still reassembles."""
    if not tail:
        return False
    if tail in ("`", "``", _FENCE_TICKS):
        return True
    if tail.startswith(_FENCE_TICKS) and "\n" not in tail:
        return True
    return False


def _parse_fenced_body(body: str) -> tuple[str, dict[str, object]] | None:
    """Parse a fenced-JSON body into (name, params), or None if the
    body isn't a recognizable tool-call shape. `strict=False` allows
    raw control chars in string literals (some models leak them)."""
    try:
        obj = json.loads(body, strict=False)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    name_key = next(
        (k for k in _NAME_KEYS if isinstance(obj.get(k), str)),
        None,
    )
    if name_key is None:
        return None
    name = obj[name_key]
    raw_args: object = None
    for k in _ARGS_KEYS:
        v = obj.get(k)
        if isinstance(v, dict):
            raw_args = v
            break
    if raw_args is None:
        raw_args = {k: v for k, v in obj.items() if k != name_key}
    if not isinstance(raw_args, dict):
        return None
    return name, {str(k): v for k, v in raw_args.items()}


class FencedJsonFlavor:
    name = "fenced_json"

    def find_sentinel(self, buf: str) -> int | None:
        m = _FENCE_OPEN_RE.search(buf)
        return m.start() if m is not None else None

    def try_close(self, buf: str) -> CloseResult:
        open_m = _FENCE_OPEN_RE.match(buf)
        if open_m is None:
            return Pending()
        body_start = open_m.end()
        close_idx = buf.find(_FENCE_TICKS, body_start)
        if close_idx < 0:
            return Pending()
        body = buf[body_start:close_idx].strip()
        end = close_idx + len(_FENCE_TICKS)
        tail = buf[end:]
        parsed = _parse_fenced_body(body)
        if parsed is None:
            # Whole fence (opener through closer) plus any tail flushed
            # as text -- it looked like a tool call but wasn't.
            return NotTool(flushed_text=buf[:end] + tail)
        tool_name, params = parsed
        log.info("inline-fenced-JSON tool call recovered: %s(%s)", tool_name, params)
        return Closed(chunk=_synthesize_tool_use(tool_name, params), tail=tail)

    def trailing_hold(self, buf: str) -> int:
        """Hold back any trailing partial of the fence opener."""
        max_hold = 0
        for i in range(1, min(_FENCE_PREFIX_MAX, len(buf)) + 1):
            tail = buf[-i:]
            if _FENCE_OPEN_RE.match(tail + "\n") or _looks_like_fence_prefix(tail):
                if i > max_hold:
                    max_hold = i
        return max_hold
