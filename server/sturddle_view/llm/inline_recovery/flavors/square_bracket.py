"""Square-bracket flavor: `[tool_name key: 'val' key: 25]`.

Wild-sample shape: a bracketed call with space-separated `key: value`
pairs. Values may be quoted (single or double) strings or bare JSON
literals (numbers, booleans, null).

Built only when `tool_names` is non-empty -- the sentinel needs a
name list to validate against (raw `[` is too generic).
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from ..protocol import Closed, CloseResult, Pending, Unparseable
from ...inline_tool_calls import (
    _IDENT,
    _QUOTE_CHARS,
    _coerce_literal,
    _synthesize_tool_use,
    log_recovered,
)

log = logging.getLogger(__name__)

_OPEN = "["
_CLOSE = "]"
_KV_SEP = ":"
_SHAPE = "square-bracket"


def _build_open_pattern(tool_names: Iterable[str]) -> re.Pattern[str] | None:
    """`[<name>` followed by whitespace or `]`. Longest names first so
    a name that is a prefix of another doesn't shadow."""
    names = [re.escape(n) for n in tool_names if n]
    if not names:
        return None
    alt = "|".join(sorted(set(names), key=len, reverse=True))
    return re.compile(r"\[(" + alt + r")(?=[\s\]])")


_TRAILING_OPEN_IDENT_RE = re.compile(r"\[" + _IDENT + r"$")


def _find_close(buf: str, start: int) -> int | None:
    """Index just past the `]` that closes the call at `start - 1`.
    Quote-aware. Nested `[...]` not expected at the args level but we
    track depth defensively."""
    depth = 1
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
        if ch in _QUOTE_CHARS:
            in_str = ch
            i += 1
            continue
        if ch == _OPEN:
            depth += 1
        elif ch == _CLOSE:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _tokenize_body(body: str) -> list[str] | None:
    """Split `body` into top-level whitespace-separated tokens, where
    a token is either `key:`, a quoted string, or a bare run of
    non-whitespace chars. Returns None on lex failure (unbalanced
    quote). Quote-aware."""
    tokens: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch.isspace():
            i += 1
            continue
        if ch in _QUOTE_CHARS:
            end = _scan_quoted(body, i)
            if end is None:
                return None
            tokens.append(body[i:end])
            i = end
            continue
        # Bare token: read until whitespace or quote.
        start = i
        while i < n and not body[i].isspace() and body[i] not in _QUOTE_CHARS:
            i += 1
        tokens.append(body[start:i])
    return tokens


def _scan_quoted(s: str, start: int) -> int | None:
    """Index just past the closing quote that matches `s[start]`."""
    quote = s[start]
    i = start + 1
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return None


def _parse_kv_tokens(tokens: list[str]) -> dict[str, object] | None:
    """Walk a token list expecting `key:` followed by a value token,
    repeated. Returns the parsed dict or None on shape failure.
    Trailing-colon shape (`key:`, separate value token) is required
    -- `key:value` glued together is rejected."""
    out: dict[str, object] = {}
    i = 0
    n = len(tokens)
    while i < n:
        key_tok = tokens[i]
        if not key_tok.endswith(_KV_SEP) or len(key_tok) < 2:
            return None
        key = key_tok[:-1]
        if not key.isidentifier():
            return None
        if i + 1 >= n:
            return None  # dangling key with no value
        out[key] = _coerce_literal(tokens[i + 1])
        i += 2
    return out or None


class SquareBracketFlavor:
    name = "square_bracket"

    def __init__(self, tool_names: Iterable[str]):
        self._tool_names = tuple(n for n in tool_names if n)
        self._open_re = _build_open_pattern(self._tool_names)

    @property
    def enabled(self) -> bool:
        return self._open_re is not None

    def find_sentinel(self, buf: str) -> int | None:
        if self._open_re is None:
            return None
        m = self._open_re.search(buf)
        return m.start() if m is not None else None

    def try_close(self, buf: str) -> CloseResult:
        """`buf` starts at `[<name>`. Parse:
          [<name>] | [<name> <args>]
        with <args> a space-separated `key: value` sequence."""
        if self._open_re is None:
            return Pending()
        m = self._open_re.match(buf)
        if m is None:
            return Pending()
        name_end = m.end()
        if name_end >= len(buf):
            return Pending()
        # Empty-args form: `[name]`.
        if buf[name_end] == _CLOSE:
            log_recovered(log, _SHAPE, m.group(1), {})
            return Closed(
                chunk=_synthesize_tool_use(m.group(1), {}),
                tail=buf[name_end + 1:],
            )
        close = _find_close(buf, name_end)
        if close is None:
            return Pending()
        body = buf[name_end:close - 1].strip()
        tokens = _tokenize_body(body)
        if tokens is None:
            return Unparseable(consumed=close)
        params = _parse_kv_tokens(tokens)
        if params is None:
            return Unparseable(consumed=close)
        log_recovered(log, _SHAPE, m.group(1), params)
        return Closed(
            chunk=_synthesize_tool_use(m.group(1), params),
            tail=buf[close:],
        )

    def trailing_hold(self, buf: str) -> int:
        """Hold back any trailing partial that could complete into
        `[<name>` on the next chunk. Conservative -- hold any in-progress
        bracketed identifier."""
        m = _TRAILING_OPEN_IDENT_RE.search(buf)
        if m is not None:
            return len(m.group(0))
        if buf.endswith(_OPEN):
            return 1
        return 0
