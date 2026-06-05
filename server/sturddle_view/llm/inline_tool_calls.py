"""Shared parsing utilities for inline tool-call recovery.

Some local models stream tool calls as prose instead of using the
OpenAI tool_call protocol. The streaming state machine and per-shape
flavors live in the `inline_recovery` package; this module holds the
pure parsing helpers they share -- XML/call-syntax parsers, the
balanced-bracket scanner, and tool_use synthesis.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import uuid
from typing import Iterable

from ..env_utils import env_int
from .base import ProviderChunk


# UUID hex slice length for synthetic tool_use_id; 12 chars = 48 bits.
_DEFAULT_INLINE_ID_LEN = 12
INLINE_ID_LEN = env_int("SV_AI_INLINE_TOOL_ID_LEN", _DEFAULT_INLINE_ID_LEN)

# Shared lexical primitives.
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_QUOTE_CHARS = frozenset(('"', "'"))
_BRACKET_OPENERS = "({["
_BRACKET_CLOSERS = ")}]"

# XML tool-call shape. `<tool_call>` trailer is optional.
_TOOL_CALL_RE = re.compile(
    rf"<function=(?P<name>{_IDENT})>\s*"
    r"(?P<body>.*?)"
    r"</function>\s*"
    r"(?:</tool_call>\s*)?",
    re.DOTALL,
)
_PARAM_RE = re.compile(
    rf"<parameter=(?P<key>{_IDENT})>\s*"
    r"(?P<val>.*?)"
    r"\s*</parameter>",
    re.DOTALL,
)


def _coerce_param(raw: str) -> object:
    """Coerce a parameter value string. Bare numbers / bools / lists
    parse as JSON; everything else stays a string. Matters because the
    XML protocol is type-blind but tool schemas often require ints
    (e.g. `analyze(depth=12)`)."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _parse_tool_call(xml: str) -> tuple[str, dict[str, object], int] | None:
    """Return (name, input, end_offset) or None on parse failure.
    `end_offset` is the position just past the matched XML, so the
    caller can slice any trailing prose without re-searching."""
    m = _TOOL_CALL_RE.search(xml)
    if not m:
        return None
    body = m.group("body")
    params: dict[str, object] = {}
    for pm in _PARAM_RE.finditer(body):
        params[pm.group("key")] = _coerce_param(pm.group("val").strip())
    return m.group("name"), params, m.end()


def _synthesize_tool_use(name: str, params: dict[str, object]) -> ProviderChunk:
    return ProviderChunk(
        kind="tool_use",
        tool_use_id=f"inline-{uuid.uuid4().hex[:INLINE_ID_LEN]}",
        tool_name=name,
        tool_input=params,
    )


def log_recovered(
    log: logging.Logger, shape: str, name: str, params: dict[str, object],
) -> None:
    """Uniform recovery log across flavors. `shape` is the flavor tag
    (e.g. "XML", "call"). Passed each flavor's own module logger so the
    record keeps that flavor's logger name."""
    log.info("inline-%s tool call recovered: %s(%s)", shape, name, params)


# ---------- Call-syntax recovery --------------------------------------


_BRACKET_CLOSE = {"(": ")", "{": "}"}
_BARE_KEY_RE = re.compile(rf"([{{,]\s*)({_IDENT})(\s*:)")


def _build_name_pattern(tool_names: Iterable[str]) -> re.Pattern[str] | None:
    """Compile a regex matching any of `tool_names` as a word, optionally
    preceded by `call:`, followed by `(` or `{`. Returns None when empty
    so the caller can short-circuit."""
    names = [re.escape(n) for n in tool_names if n]
    if not names:
        return None
    alt = "|".join(sorted(set(names), key=len, reverse=True))
    return re.compile(r"(?:\bcall:)?\b(" + alt + r")\s*([({])")


def _find_balanced_close(text: str, open_idx: int) -> int | None:
    """Index just past the bracket that balances `text[open_idx]`,
    or None if not closed. Quote-aware so brackets inside strings
    don't break the count."""
    opener = text[open_idx]
    closer = _BRACKET_CLOSE[opener]
    depth = 0
    i = open_idx
    n = len(text)
    in_str: str | None = None
    while i < n:
        ch = text[i]
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
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _quote_bare_keys(body: str) -> str:
    """Quote bare-identifier dict keys so json.loads can parse
    `{move: "e5"}` -> `{"move": "e5"}`. Matches only after `{` or `,`."""
    return _BARE_KEY_RE.sub(r'\1"\2"\3', body)


def _parse_call_args(body: str, opener: str = "(") -> dict[str, object] | None:
    """Permissive parser cascade for `name(body)` / `name{body}` args.
    Returns a normalized dict or None on total failure. Positional args
    are not supported -- our tools all take named params.

    `opener` is the bracket type at the call site. When `{`, we re-wrap
    the (already-stripped) body in braces for the bare-key JSON retry
    -- the round trip is intentional: the outer braces were peeled by
    the caller, so we restore them for dict-shape parsing.
    """
    body = body.strip()
    if not body:
        return {}

    for parser in (json.loads, ast.literal_eval):
        try:
            val = parser(body)
        except (ValueError, SyntaxError):
            continue
        if isinstance(val, dict):
            return {str(k): v for k, v in val.items()}
        return None  # non-dict literal -- no positional dispatch

    if opener == "{":
        wrapped = "{" + body + "}"
        try:
            val = json.loads(_quote_bare_keys(wrapped))
            if isinstance(val, dict):
                return {str(k): v for k, v in val.items()}
        except (ValueError, SyntaxError):
            pass
        try:
            val = ast.literal_eval(wrapped)
            if isinstance(val, dict):
                return {str(k): v for k, v in val.items()}
        except (ValueError, SyntaxError):
            pass

    parts = _split_top_level(body)
    out: dict[str, object] = {}
    for part in parts:
        part = part.strip()
        if not part:
            continue
        sep = _find_top_level_kv(part)
        if sep is None:
            return None  # positional arg in unparseable body -- give up
        key = part[:sep].strip()
        # Strip quotes from the key if the model emitted `"k"=v`.
        if len(key) >= 2 and key[0] == key[-1] and key[0] in _QUOTE_CHARS:
            key = key[1:-1]
        val_raw = part[sep + 1:].strip()
        out[key] = _coerce_literal(val_raw)
    return out or None


def _iter_top_level(s: str):
    """Yield (index, char) for every character of `s` that is at depth
    zero (not inside quotes or nested brackets). Quote-aware: handles
    backslash-escaped chars inside strings."""
    depth = 0
    in_str: str | None = None
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
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
        elif ch in _BRACKET_OPENERS:
            depth += 1
        elif ch in _BRACKET_CLOSERS:
            depth -= 1
        elif depth == 0:
            yield i, ch
        i += 1


def _split_top_level(s: str) -> list[str]:
    """Split on commas not inside quotes or nested brackets."""
    parts: list[str] = []
    start = 0
    for i, ch in _iter_top_level(s):
        if ch == ",":
            parts.append(s[start:i])
            start = i + 1
    parts.append(s[start:])
    return parts


def _find_top_level_kv(s: str) -> int | None:
    """Index of the first `=` or `:` not inside quotes/nested brackets,
    or None. Both serve as key/value separators in real-world models;
    `=` is kwarg shape, `:` is dict-ish shape without braces."""
    for i, ch in _iter_top_level(s):
        if ch == "=" or ch == ":":
            return i
    return None


def _coerce_literal(raw: str) -> object:
    """Single-token coerce: JSON (handles "...\\"...") -> Python literal
    (True/False/None, single-quoted strings) -> stripped-quote fallback
    -> bare string."""
    s = raw.strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError):
        pass
    if len(s) >= 2 and s[0] == s[-1] and s[0] in _QUOTE_CHARS:
        return s[1:-1]
    return s
