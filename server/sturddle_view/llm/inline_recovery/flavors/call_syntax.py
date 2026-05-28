"""Call-syntax flavor: `name(args)` / `name{args}` / `call:name(...)`.

Currently a wrapper around the legacy `_try_recover_first_call` plus
the legacy trailing-hold heuristics. Step 3 of the refactor will
replace this with a native implementation.

Built only when `tool_names` is non-empty. The State leaves this
flavor out of the registry otherwise -- call-syntax can't match
without a name list.
"""
from __future__ import annotations

import logging
from typing import Iterable

from ..protocol import Closed, CloseResult, Pending, Unparseable
from ...inline_tool_calls import (
    _CallNone,
    _CallUnfinished,
    _CallUnparseable,
    _TRAILING_CALL_PREFIX_RE,
    _TRAILING_IDENT_RE,
    _build_name_pattern,
    _synthesize_tool_use,
    _try_recover_first_call,
)

log = logging.getLogger(__name__)


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
        result = _try_recover_first_call(buf, self._name_re)
        if isinstance(result, _CallNone):
            return None
        # Unfinished / Unparseable / Match all carry match_start.
        return result.match_start

    def try_close(self, buf: str) -> CloseResult:
        """`buf` starts at the sentinel (caller has sliced)."""
        if self._name_re is None:
            return Pending()
        result = _try_recover_first_call(buf, self._name_re)
        if isinstance(result, _CallNone):
            # Should not happen given caller invariant, but be safe.
            return Pending()
        if isinstance(result, _CallUnfinished):
            return Pending()
        if isinstance(result, _CallUnparseable):
            return Unparseable(consumed=result.match_end)
        # _CallMatch
        log.info(
            "inline-call tool call recovered: %s(%s)",
            result.name, result.params,
        )
        return Closed(
            chunk=_synthesize_tool_use(result.name, result.params),
            tail=buf[result.match_end:],
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
            tail = _TRAILING_IDENT_RE.search(buf)
            if tail:
                max_prefix = len(tail.group(0))
        call_tail = _TRAILING_CALL_PREFIX_RE.search(buf)
        if call_tail and len(call_tail.group(0)) > max_prefix:
            max_prefix = len(call_tail.group(0))
        return max_prefix
