"""Jinja-pipe flavor: `{{ "tool_name" | json_call:k=v,... }}`.

Wild-sample shape seen from a model emitting tool calls as Jinja-style
template expressions with a `json_call:` filter. The tool name is a
JSON-quoted string before the pipe; args are kwarg-form after the
colon.

Built only when `tool_names` is non-empty -- the sentinel is too
generic (`{{`) to scan for without a name list to validate against.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from ..protocol import Closed, CloseResult, Pending, Unparseable
from ...inline_tool_calls import (
    _parse_call_args,
    _synthesize_tool_use,
    log_recovered,
)

log = logging.getLogger(__name__)

_SHAPE = "jinja-pipe"
_OPEN = "{{"
_CLOSE = "}}"
_FILTER = "json_call:"
_WS_RE = re.compile(r"[ \t\r\n]*")


def _find_closing_quote(buf: str, open_idx: int) -> int | None:
    """Index of the matching closing double-quote starting at
    `buf[open_idx] == '\"'`. Handles backslash escapes."""
    i = open_idx + 1
    n = len(buf)
    while i < n:
        ch = buf[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == '"':
            return i
        i += 1
    return None


def _consume_or_pending(buf: str) -> CloseResult:
    """Decide whether to give up on this `{{` opener now or wait for
    more data. If `}}` is already in `buf`, consume through it (the
    whole expression isn't a tool call). Otherwise, consume just the
    `{{` so scanning resumes -- avoids holding a buffer indefinitely
    on a non-tool template expression."""
    close_idx = buf.find(_CLOSE)
    if close_idx >= 0:
        return Unparseable(consumed=close_idx + len(_CLOSE))
    return Unparseable(consumed=len(_OPEN))


def _find_balanced_close_pair(buf: str, start: int) -> int | None:
    """Index just past `}}` that balances the opening `{{` at `start`.
    Quote-aware so braces inside string literals don't break the
    count. Returns None if not closed."""
    depth = 1  # caller has already consumed the opening `{{`
    i = start
    n = len(buf)
    in_str: str | None = None
    while i < n:
        ch = buf[i]
        if in_str is not None:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = ch
            i += 1
            continue
        if ch == "{" and i + 1 < n and buf[i + 1] == "{":
            depth += 1
            i += 2
            continue
        if ch == "}" and i + 1 < n and buf[i + 1] == "}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return None


class JinjaPipeFlavor:
    name = "jinja_pipe"

    def __init__(self, tool_names: Iterable[str]):
        self._tool_names = frozenset(n for n in tool_names if n)

    @property
    def enabled(self) -> bool:
        return bool(self._tool_names)

    def find_sentinel(self, buf: str) -> int | None:
        """Find the next `{{` that looks like a real jinja-pipe call
        (`{{ ws "name" ...`). Plain `{{` without that prefix is left
        for other flavors -- jinja-pipe shouldn't grab doubled-brace
        wrappers that aren't actually filter expressions."""
        start = 0
        while start < len(buf):
            i = buf.find(_OPEN, start)
            if i < 0:
                return None
            if self._looks_like_jinja_call(buf, i):
                return i
            start = i + 1
        return None

    def _looks_like_jinja_call(self, buf: str, idx: int) -> bool:
        """Cheap prefix check: after `{{`, optional whitespace, then a
        double-quote. Conservative enough to skip `{{` wrappers
        around bare JSON (which start with `{{"...`, no space).

        If the trailing content past `{{` isn't yet long enough to
        decide, return True so the state machine enters capture mode
        and waits -- the full `try_close` will sort it out."""
        body_start = idx + len(_OPEN)
        if body_start >= len(buf):
            return True  # too short to disprove; let try_close handle it
        m_ws = _WS_RE.match(buf, body_start)
        if m_ws.end() == body_start:
            # No whitespace after `{{` -> almost certainly not a
            # jinja-pipe expression (real ones use `{{ "..." | ...}}`).
            return False
        if m_ws.end() >= len(buf):
            return True
        return buf[m_ws.end()] == '"'

    def try_close(self, buf: str) -> CloseResult:
        """`buf` starts with `{{`. Parse:
          ws "name" ws | ws json_call: <args> }}
        Return Pending while more data may resolve the shape;
        Unparseable to flush the opener and resume scanning when the
        prefix can't be a tool call; Closed on success.
        """
        if not buf.startswith(_OPEN):
            return Unparseable(consumed=len(_OPEN))

        body_start = len(_OPEN)
        # Skip whitespace after `{{`.
        m_ws = _WS_RE.match(buf, body_start)
        name_start = m_ws.end()

        # Need at least one char to know whether the name shape begins.
        if name_start >= len(buf):
            return Pending()
        if buf[name_start] != '"':
            # Not a jinja-pipe tool call (no quoted name).
            return _consume_or_pending(buf)

        # Parse the quoted name.
        name_end = _find_closing_quote(buf, name_start)
        if name_end is None:
            return Pending()
        tool_name = buf[name_start + 1:name_end]
        if tool_name not in self._tool_names:
            # Quoted string isn't one of our tool names.
            return _consume_or_pending(buf)

        # Skip ws then `|` then ws then `json_call:`.
        cursor = name_end + 1
        m_ws = _WS_RE.match(buf, cursor)
        cursor = m_ws.end()
        if cursor >= len(buf):
            return Pending()
        if buf[cursor] != "|":
            return _consume_or_pending(buf)
        cursor += 1
        m_ws = _WS_RE.match(buf, cursor)
        cursor = m_ws.end()
        if not buf.startswith(_FILTER, cursor):
            # Must see the filter name; might be incomplete.
            if _FILTER.startswith(buf[cursor:]):
                return Pending()
            return _consume_or_pending(buf)
        cursor += len(_FILTER)

        # Find balanced `}}`.
        close_end = _find_balanced_close_pair(buf, cursor)
        if close_end is None:
            return Pending()
        args_body = buf[cursor:close_end - len(_CLOSE)].strip()
        params = _parse_call_args(args_body)
        if params is None:
            # Filter found and braces balanced but args failed to
            # parse -- flush the whole span as text and move on.
            return Unparseable(consumed=close_end)
        log_recovered(log, _SHAPE, tool_name, params)
        return Closed(
            chunk=_synthesize_tool_use(tool_name, params),
            tail=buf[close_end:],
        )

    def trailing_hold(self, buf: str) -> int:
        """Hold back a single trailing `{` that could complete into
        the `{{` opener on the next chunk. A full `{{` would have
        been found by find_sentinel, so it never reaches here."""
        if buf.endswith("{") and not buf.endswith("{{"):
            return 1
        return 0
