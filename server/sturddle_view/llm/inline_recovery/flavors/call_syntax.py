"""Call-syntax flavor: `name(args)` / `name{args}` / `call:name(...)`.

Built only when `tool_names` is non-empty -- the State leaves this
flavor out of the registry otherwise.

The state-machine layer (find_sentinel / try_close / trailing_hold)
is native here. Parsing helpers (`_parse_call_args`,
`_find_balanced_close`, `_build_name_pattern`) are reused from the
legacy module; they are pure utilities, orthogonal to the refactor.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from ..protocol import Closed, CloseResult, Pending, Unparseable
from ...inline_tool_calls import (
    _IDENT,
    _build_name_pattern,
    _find_balanced_close,
    _parse_call_args,
    _synthesize_tool_use,
)

log = logging.getLogger(__name__)

# Trailing prefix of `call:` plus optional identifier; covers
# in-progress `call:<name>` so we don't flush the decoration before
# the name chunk arrives.
_TRAILING_CALL_PREFIX_RE = re.compile(
    r"(?:c|ca|cal|call|call:|call:" + _IDENT + r")$"
)
_TRAILING_IDENT_RE = re.compile(_IDENT + r"$")


class CallSyntaxFlavor:
    name = "call_syntax"

    def __init__(self, tool_names: Iterable[str]):
        self._tool_names = tuple(n for n in tool_names if n)
        self._name_re = _build_name_pattern(self._tool_names)

    @property
    def enabled(self) -> bool:
        return self._name_re is not None

    def find_sentinel(self, buf: str) -> int | None:
        if self._name_re is None:
            return None
        m = self._name_re.search(buf)
        return m.start() if m is not None else None

    def try_close(self, buf: str) -> CloseResult:
        """`buf` starts at the sentinel (caller has sliced). Returns:
          - Pending if the bracket hasn't balanced yet,
          - Unparseable when the bracket closed but args didn't parse,
          - Closed on a clean match.
        """
        if self._name_re is None:
            return Pending()
        m = self._name_re.match(buf)
        if m is None:
            # Caller invariant violated -- treat as no-op and wait.
            return Pending()
        open_idx = m.start(2)
        close = _find_balanced_close(buf, open_idx)
        if close is None:
            return Pending()
        body = buf[open_idx + 1:close - 1]
        params = _parse_call_args(body, opener=buf[open_idx])
        if params is None:
            return Unparseable(consumed=close)
        log.info("inline-call tool call recovered: %s(%s)", m.group(1), params)
        return Closed(
            chunk=_synthesize_tool_use(m.group(1), params),
            tail=buf[close:],
        )

    def trailing_hold(self, buf: str) -> int:
        """Hold back any trailing partial that could complete into a
        tool name or `call:name` prefix on the next chunk."""
        max_prefix = 0
        for name in self._tool_names:
            for i in range(1, min(len(name), len(buf)) + 1):
                if name.startswith(buf[-i:]) and i > max_prefix:
                    max_prefix = i
        if max_prefix == 0:
            ident_tail = _TRAILING_IDENT_RE.search(buf)
            if ident_tail:
                max_prefix = len(ident_tail.group(0))
        call_tail = _TRAILING_CALL_PREFIX_RE.search(buf)
        if call_tail and len(call_tail.group(0)) > max_prefix:
            max_prefix = len(call_tail.group(0))
        return max_prefix
