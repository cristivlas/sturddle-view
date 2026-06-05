"""XML inline tool-call flavor: `<function=name>...</function>`.

Native implementation. Parsing helpers (`_parse_tool_call`,
`_synthesize_tool_use`) are the shared pure utilities in
`..inline_tool_calls`.
"""
from __future__ import annotations

import logging

from ..protocol import Closed, CloseResult, Pending
from ...inline_tool_calls import _parse_tool_call, _synthesize_tool_use

log = logging.getLogger(__name__)

_SENTINEL = "<function="


class XmlFlavor:
    name = "xml"

    def find_sentinel(self, buf: str) -> int | None:
        i = buf.find(_SENTINEL)
        return i if i >= 0 else None

    def try_close(self, buf: str) -> CloseResult:
        result = _parse_tool_call(buf)
        if result is None:
            return Pending()
        name, params, end = result
        log.info("inline-XML tool call recovered: %s(%s)", name, params)
        return Closed(chunk=_synthesize_tool_use(name, params), tail=buf[end:])

    def trailing_hold(self, buf: str) -> int:
        """Hold back any trailing prefix of `<function=` so a sentinel
        split across chunks reassembles."""
        max_hold = 0
        for i in range(1, min(len(_SENTINEL), len(buf)) + 1):
            if _SENTINEL.startswith(buf[-i:]):
                max_hold = i
        return max_hold
