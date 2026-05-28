"""Bare-JSON flavor: `{"name": "tool", "parameters": {...}}` in prose.

Wild shape from Llama 3 and similar: the model's training-time tool
schema (a JSON object with `name` and `parameters`/`arguments`) leaks
out as raw text when the structured channel is unavailable.

Sentinel is `{`, which is extremely common in prose. The flavor
validates aggressively: brackets must balance (quote-aware), the
body must parse as JSON, the result must be a dict with a string
`name` field in `tool_names`, and either `parameters` or `arguments`
must be a dict. Any miss returns Unparseable(consumed=1) so the
single `{` is flushed as text and scanning resumes.

Built only when `tool_names` is non-empty.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, Mapping, Optional, Sequence

from ..protocol import Closed, CloseResult, Pending, Unparseable
from ...inline_tool_calls import _synthesize_tool_use

log = logging.getLogger(__name__)

_OPEN = "{"
_CLOSE = "}"
_QUOTE_CHARS = ('"', "'")
_NAME_KEY = "name"
_ARGS_KEYS = ("parameters", "arguments")


def _try_parse_object(span: str) -> Optional[dict]:
    """Parse `span` as JSON; return it only if it's a dict.
    `strict=False` lets raw control characters inside string literals
    through -- some models leak `\\x19`-style bytes that strict JSON
    rejects."""
    try:
        obj = json.loads(span, strict=False)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _find_balanced_close(buf: str) -> int | None:
    """Index just past the `}` that balances `buf[0] == '{'`.
    Quote-aware so braces inside string literals don't break the
    count. Returns None if not closed."""
    if not buf or buf[0] != _OPEN:
        return None
    depth = 0
    i = 0
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


class BareJsonFlavor:
    name = "bare_json"

    def __init__(
        self,
        tool_names: Iterable[str],
        *,
        tool_schemas: Optional[Mapping[str, Sequence[str]]] = None,
    ):
        self._tool_names = frozenset(n for n in tool_names if n)
        # Stored as a plain dict so lookups don't go through a custom
        # Mapping implementation per call.
        self._schemas: dict[str, tuple[str, ...]] = {
            k: tuple(v) for k, v in (tool_schemas or {}).items()
        }

    @property
    def enabled(self) -> bool:
        return bool(self._tool_names)

    def find_sentinel(self, buf: str) -> int | None:
        i = buf.find(_OPEN)
        return i if i >= 0 else None

    def try_close(self, buf: str) -> CloseResult:
        """`buf` starts with `{`. Balance braces, parse as JSON,
        validate tool-call shape. False positives flush as a single
        `{` so prose with stray braces isn't mangled.

        Also accepts a doubled-brace wrapper (`{{...}}`) seen from
        some Llama 3 outputs: when the outer balanced span doesn't
        parse but the inner `{...}` does, the inner is used and the
        outer braces are consumed as part of the recovered span."""
        if not buf.startswith(_OPEN):
            return Unparseable(consumed=1)
        close_end = _find_balanced_close(buf)
        if close_end is None:
            return Pending()
        obj = _try_parse_object(buf[:close_end])
        if obj is None and buf.startswith(_OPEN + _OPEN):
            # Doubled-brace wrapper: peel one `{` from each side.
            inner_end = _find_balanced_close(buf[1:])
            if inner_end is not None:
                obj = _try_parse_object(buf[1:1 + inner_end])
        if obj is None:
            return Unparseable(consumed=1)
        raw_name = obj.get(_NAME_KEY)
        if not isinstance(raw_name, str) or raw_name not in self._tool_names:
            return Unparseable(consumed=1)
        params = self._extract_params(obj, raw_name)
        if params is None:
            return Unparseable(consumed=1)
        log.info("inline-bare-JSON tool call recovered: %s(%s)", raw_name, params)
        return Closed(
            chunk=_synthesize_tool_use(raw_name, params),
            tail=buf[close_end:],
        )

    def _extract_params(self, obj: dict, tool_name: str) -> Optional[dict]:
        """Resolve the args dict from a parsed JSON object. Three
        shapes accepted:
          1. `parameters` / `arguments` is a dict -> use as-is.
          2. `parameters` is a scalar (str / int / bool) or 1-element
             list -> map to the tool's first param name (requires
             schema).
          3. `parameters` is a list of N values -> map element-wise to
             the first N schema params (requires schema).
        Returns None on shape mismatch."""
        for k in _ARGS_KEYS:
            v = obj.get(k)
            if isinstance(v, dict):
                return {str(kk): vv for kk, vv in v.items()}
        # Positional fallback -- needs a schema.
        schema = self._schemas.get(tool_name)
        if not schema:
            return None
        for k in _ARGS_KEYS:
            v = obj.get(k)
            if v is None:
                continue
            return _positional_to_kwargs(v, schema)
        return None

    def trailing_hold(self, buf: str) -> int:
        """Hold back a trailing `{` so it can be paired with the next
        chunk for balance scanning."""
        return 1 if buf.endswith(_OPEN) else 0


_DEQUOTE_CHARS = ('"', "'")


def _dequote(s: str) -> str:
    """Strip a matching pair of surrounding single or double quotes."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in _DEQUOTE_CHARS:
        return s[1:-1]
    return s


def _positional_to_kwargs(value: object, schema: Sequence[str]) -> Optional[dict]:
    """Map a positional `parameters` value to a kwargs dict using
    `schema` (ordered param names). Accepts a scalar (string/number/
    bool), a list of scalars, or a single-element bracketed-string
    list (e.g. `"[c4]"`). Returns None on shape mismatch."""
    if isinstance(value, list):
        values = list(value)
    elif isinstance(value, str):
        # Some models emit `"parameters": "[c4]"` -- a bracketed list
        # as a string. Strip the brackets and split on commas; each
        # element becomes a (trimmed, dequoted) string scalar.
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inner = stripped[1:-1].strip()
            if not inner:
                values = []
            else:
                values = [_dequote(p.strip()) for p in inner.split(",")]
        else:
            values = [value]
    else:
        # Scalar bool/int/float: wrap as one positional.
        values = [value]
    if not values or len(values) > len(schema):
        return None
    return {schema[i]: values[i] for i in range(len(values))}
