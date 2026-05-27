"""Inline tool-call recovery for models that emit tool calls as prose.

Three patterns supported:

1. XML shape: `<function=name>...</function>`.
2. Call-syntax shape: `name(args)` or `name{args}` with args in JSON,
   Python-dict, or kwarg form. Requires the caller to pass a
   `tool_names` set so the wrapper knows which names to watch.
3. Fenced-JSON shape: ```json {"tool"|"name": ..., "args"|"arguments":
   {...}} ``` -- accepts either key convention.

For each pattern, the wrapper buffers the matched span, parses it, and
emits a synthetic `tool_use` chunk in place of the literal text. On
parse failure or unclosed buffer at stream end, swallowed text is
flushed back as a normal text chunk -- the user sees what the model
emitted instead of silent loss.

REFACTORING NOTE (deferred). `recover_inline_tool_calls` is a flat
state machine carrying multiple parallel buffers (xml_buf,
fence_buf, call_buf, pre_buf) and capture flags. Each new flavor
adds another buf+flag pair and another branch in the main loop.
At a fourth flavor it'll be cheaper to extract a `Flavor` protocol
(detect_sentinel + try_close) and a state class that walks a flavor
registry. Today's shape is fine for three flavors; revisit before
adding DSML / Qwen / etc."""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Iterable

from .base import ProviderChunk


log = logging.getLogger(__name__)


_SENTINEL = "<function="
# Opening fence prefix (`` ``` ``). The optional language tag (e.g.
# `json`) is matched separately so casing/whitespace variants pass.
_FENCE_PREFIX = "```"
_FENCE_OPEN_RE = re.compile(r"```[ \t]*[Jj][Ss][Oo][Nn][ \t]*\n")
_FENCE_CLOSE = "```"
# UUID hex slice length for synthetic tool_use_id; 12 chars = 48 bits.
_DEFAULT_INLINE_ID_LEN = 12
INLINE_ID_LEN = int(os.environ.get("SV_AI_INLINE_TOOL_ID_LEN", _DEFAULT_INLINE_ID_LEN))

# Shared lexical primitives.
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_IDENT_RE = re.compile(_IDENT)
_TRAILING_IDENT_RE = re.compile(_IDENT + r"$")
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


# Streaming-helper outcomes. Dataclasses (not tuples) so unpacking
# sites can branch on isinstance without # type: ignore noise.


@dataclass(frozen=True)
class _CallNone:
    """No `name(...)` / `name{...}` shape found in the buffer."""


@dataclass(frozen=True)
class _CallUnfinished:
    """Match found but bracket isn't closed yet; hold and wait."""
    match_start: int


@dataclass(frozen=True)
class _CallUnparseable:
    """Match closed but args couldn't be parsed; flush as text."""
    match_start: int
    match_end: int


@dataclass(frozen=True)
class _CallMatch:
    """Complete and parsed."""
    match_start: int
    match_end: int
    name: str
    params: dict[str, object]


_CallRecovery = _CallNone | _CallUnfinished | _CallUnparseable | _CallMatch


def _try_recover_first_call(
    buf: str, name_re: re.Pattern[str],
) -> _CallRecovery:
    """Inspect `buf` for the earliest `name(args)` / `name{args}` shape."""
    m = name_re.search(buf)
    if m is None:
        return _CallNone()
    open_idx = m.start(2)
    close = _find_balanced_close(buf, open_idx)
    if close is None:
        return _CallUnfinished(m.start())
    body = buf[open_idx + 1:close - 1]
    params = _parse_call_args(body, opener=buf[open_idx])
    if params is None:
        return _CallUnparseable(m.start(), close)
    return _CallMatch(m.start(), close, m.group(1), params)


# ---------- Top-level wrapper -----------------------------------------


def _try_close_xml(xml_buf: str) -> tuple[ProviderChunk, str] | None:
    """If `xml_buf` contains a complete `<function=...>...</function>`,
    return (tool_use_chunk, trailing_text). Else None."""
    result = _parse_tool_call(xml_buf)
    if result is None:
        return None
    name, params, end = result
    log.info("inline-XML tool call recovered: %s(%s)", name, params)
    return _synthesize_tool_use(name, params), xml_buf[end:]


def _try_close_fenced_json(buf: str):
    """If `buf` starts with a ``` ```json `` opener and the closing
    ``` ``` `` has arrived, return:
      - (chunk, tail) when the body parses as {"tool": str, "args": dict};
      - ("nottool", tail_with_fence) when fence closed but body isn't a
        tool call -- the caller flushes the whole fence as text;
      - None when the fence isn't closed yet.
    """
    open_m = _FENCE_OPEN_RE.match(buf)
    if open_m is None:
        return None
    body_start = open_m.end()
    close_idx = buf.find(_FENCE_CLOSE, body_start)
    if close_idx < 0:
        return None
    body = buf[body_start:close_idx].strip()
    tail = buf[close_idx + len(_FENCE_CLOSE):]
    fence_text = buf[:close_idx + len(_FENCE_CLOSE)]
    try:
        obj = json.loads(body)
    except ValueError:
        return ("nottool", fence_text + tail)
    if not isinstance(obj, dict):
        return ("nottool", fence_text + tail)
    # Accept either {tool, args} or {name, arguments}; real-world models
    # split between the two conventions.
    raw_name = obj.get("tool") if isinstance(obj.get("tool"), str) else obj.get("name")
    raw_args = obj.get("args") if isinstance(obj.get("args"), dict) else obj.get("arguments")
    if not isinstance(raw_name, str) or not isinstance(raw_args, dict):
        return ("nottool", fence_text + tail)
    name = raw_name
    params = {str(k): v for k, v in raw_args.items()}
    log.info("inline-fenced-JSON tool call recovered: %s(%s)", name, params)
    return _synthesize_tool_use(name, params), tail


async def recover_inline_tool_calls(
    upstream: AsyncIterator[ProviderChunk],
    *,
    tool_names: Iterable[str] | None = None,
) -> AsyncIterator[ProviderChunk]:
    """Async-iterator wrapper that converts inline tool calls into
    synthetic tool_use chunks. Non-text chunks pass through unchanged.

    When `tool_names` is provided, the wrapper also recovers
    `name(args)` / `name{args}` shapes for any matching name. None or
    empty disables call-syntax recovery (legacy XML path only).
    """
    name_re = _build_name_pattern(tool_names) if tool_names else None

    xml_buf = ""
    xml_capturing = False
    fence_buf = ""
    fence_capturing = False
    # Tiny pre-flush buffer: holds back any trailing chars that could
    # complete a sentinel (`<function=` or ```json) on the next chunk.
    # Independent of call-syntax recovery, so fence/XML detection works
    # without tool_names.
    pre_buf = ""
    # Holds pending text that may complete a call-syntax match; never
    # flushed until we know no match is forming.
    call_buf = ""
    # Source channel of the buffered content ("text" or "thinking").
    # Recovery runs on both; channel switch flushes the buffer first.
    channel = "text"

    async for chunk in upstream:
        is_recoverable = chunk.kind in ("text", "thinking") and chunk.text
        if not is_recoverable:
            # Non-recoverable chunk mid-buffer: flush pending content first
            # on its source channel.
            if call_buf:
                yield ProviderChunk(kind=channel, text=call_buf)
                call_buf = ""
            yield chunk
            continue

        if chunk.kind != channel and (
            call_buf or xml_capturing or fence_capturing or pre_buf
        ):
            # Channel switch with pending buffer: flush as the old channel.
            if call_buf:
                yield ProviderChunk(kind=channel, text=call_buf)
                call_buf = ""
            if xml_capturing and xml_buf:
                yield ProviderChunk(kind=channel, text=xml_buf)
                xml_buf = ""
                xml_capturing = False
            if fence_capturing and fence_buf:
                yield ProviderChunk(kind=channel, text=fence_buf)
                fence_buf = ""
                fence_capturing = False
            if pre_buf:
                yield ProviderChunk(kind=channel, text=pre_buf)
                pre_buf = ""
        channel = chunk.kind
        text = pre_buf + chunk.text
        pre_buf = ""

        if xml_capturing:
            xml_buf += text
            closed = _try_close_xml(xml_buf)
            if closed is None:
                continue
            tool_chunk, tail = closed
            yield tool_chunk
            if tail:
                yield ProviderChunk(kind=channel, text=tail)
            xml_buf = ""
            xml_capturing = False
            continue

        if fence_capturing:
            fence_buf += text
            closed = _try_close_fenced_json(fence_buf)
            if closed is None:
                continue
            fence_capturing = False
            if isinstance(closed[0], str) and closed[0] == "nottool":
                yield ProviderChunk(kind=channel, text=closed[1])
            else:
                tool_chunk, tail = closed
                yield tool_chunk
                if tail:
                    yield ProviderChunk(kind=channel, text=tail)
            fence_buf = ""
            continue

        # XML sentinel takes precedence over call-syntax.
        xml_idx = text.find(_SENTINEL)
        if xml_idx >= 0:
            prefix = call_buf + text[:xml_idx]
            if prefix:
                yield ProviderChunk(kind=channel, text=prefix)
            call_buf = ""
            xml_buf = text[xml_idx:]
            xml_capturing = True
            closed = _try_close_xml(xml_buf)
            if closed is None:
                continue
            tool_chunk, tail = closed
            yield tool_chunk
            if tail:
                yield ProviderChunk(kind=channel, text=tail)
            xml_buf = ""
            xml_capturing = False
            continue

        fence_m = _FENCE_OPEN_RE.search(text)
        if fence_m is not None:
            fence_idx = fence_m.start()
            prefix = call_buf + text[:fence_idx]
            if prefix:
                yield ProviderChunk(kind=channel, text=prefix)
            call_buf = ""
            fence_buf = text[fence_idx:]
            fence_capturing = True
            closed = _try_close_fenced_json(fence_buf)
            if closed is None:
                continue
            fence_capturing = False
            if isinstance(closed[0], str) and closed[0] == "nottool":
                yield ProviderChunk(kind=channel, text=closed[1])
            else:
                tool_chunk, tail = closed
                yield tool_chunk
                if tail:
                    yield ProviderChunk(kind=channel, text=tail)
            fence_buf = ""
            continue

        if name_re is None:
            # No call-syntax recovery: pass through on source channel.
            # First, hold back any trailing partial sentinel so a fence
            # or XML opener split across chunks can still match.
            flush, pre_buf = _split_at_sentinel_prefix(text)
            if call_buf:
                yield ProviderChunk(kind=channel, text=call_buf)
                call_buf = ""
            if flush:
                yield ProviderChunk(kind=channel, text=flush)
            continue

        call_buf += text
        while call_buf:
            result = _try_recover_first_call(call_buf, name_re)
            if isinstance(result, _CallNone):
                safe_flush, keep = _split_at_safe_boundary(call_buf, tool_names or ())
                if safe_flush:
                    yield ProviderChunk(kind=channel, text=safe_flush)
                call_buf = keep
                break
            if isinstance(result, _CallUnfinished):
                if result.match_start > 0:
                    yield ProviderChunk(kind=channel, text=call_buf[:result.match_start])
                    call_buf = call_buf[result.match_start:]
                break
            if isinstance(result, _CallUnparseable):
                yield ProviderChunk(kind=channel, text=call_buf[:result.match_end])
                call_buf = call_buf[result.match_end:]
                continue
            # _CallMatch
            if result.match_start > 0:
                yield ProviderChunk(kind=channel, text=call_buf[:result.match_start])
            log.info("inline-call tool call recovered: %s(%s)", result.name, result.params)
            yield _synthesize_tool_use(result.name, result.params)
            call_buf = call_buf[result.match_end:]

    # Stream end: flush whatever's still buffered on its source channel.
    if xml_capturing and xml_buf:
        log.warning(
            "inline-XML tool call did not close before stream end; flushing %d bytes",
            len(xml_buf),
        )
        yield ProviderChunk(kind=channel, text=xml_buf)
    if fence_capturing and fence_buf:
        log.warning(
            "inline-fenced-JSON tool call did not close before stream end; flushing %d bytes",
            len(fence_buf),
        )
        yield ProviderChunk(kind=channel, text=fence_buf)
    if pre_buf:
        yield ProviderChunk(kind=channel, text=pre_buf)
    if call_buf:
        yield ProviderChunk(kind=channel, text=call_buf)


# Trailing prefix of `call:` followed by an optional identifier;
# covers in-progress `call:<name>` so we don't flush the decoration
# before the name chunk arrives.
_TRAILING_CALL_PREFIX_RE = re.compile(
    r"(?:c|ca|cal|call|call:|call:" + _IDENT + r")$"
)


def _split_at_sentinel_prefix(buf: str) -> tuple[str, str]:
    """Split `buf` so any trailing partial of a recovery sentinel
    (`<function=` or ```` ```json ````) is held back for the next chunk.
    Lets the fence/XML detector see a split sentinel reconstituted across
    chunks. Fence-prefix match is case-insensitive on the language tag."""
    max_hold = 0
    # XML opener: literal prefix match.
    for i in range(1, min(len(_SENTINEL), len(buf)) + 1):
        if _SENTINEL.startswith(buf[-i:]) and i > max_hold:
            max_hold = i
    # Fence opener: scan tail for a prefix that could complete to
    # ``` ```[ws]json[ws]\n ```. Cheap upper-bound on the longest tail
    # we'd need to hold (3 backticks + "json" + whitespace + newline = 9).
    for i in range(1, min(9, len(buf)) + 1):
        tail = buf[-i:]
        if _FENCE_OPEN_RE.match(tail + "\n") or _looks_like_fence_prefix(tail):
            if i > max_hold:
                max_hold = i
    if max_hold == 0:
        return buf, ""
    return buf[:-max_hold], buf[-max_hold:]


def _looks_like_fence_prefix(tail: str) -> bool:
    """True iff `tail` is a strict prefix of any case variant of
    ``` ```json ```. We hold these so a fence opener split across chunks
    still triggers on reassembly."""
    if not tail:
        return False
    # Walk a canonical opener and see if tail matches its first len(tail)
    # chars in some case folding.
    for variant in ("```json", "``` json"):
        if len(tail) <= len(variant) and variant[:len(tail)].lower() == tail.lower():
            return True
    return False


def _split_at_safe_boundary(buf: str, tool_names: Iterable[str]) -> tuple[str, str]:
    """Split `buf` into (safe_to_flush, hold_for_next_chunk). Hold back
    any trailing partial that could complete into a tool name on the
    next chunk -- bare identifier prefix or `call:`-prefixed shape."""
    max_prefix = 0
    for name in tool_names:
        for i in range(1, min(len(name), len(buf)) + 1):
            if name.startswith(buf[-i:]) and i > max_prefix:
                max_prefix = i
    if max_prefix == 0:
        tail = _TRAILING_IDENT_RE.search(buf)
        if tail:
            max_prefix = len(tail.group(0))
    # Also hold back any trailing partial of `call:<identifier>`.
    call_tail = _TRAILING_CALL_PREFIX_RE.search(buf)
    if call_tail and len(call_tail.group(0)) > max_prefix:
        max_prefix = len(call_tail.group(0))
    if max_prefix == 0:
        return buf, ""
    return buf[:-max_prefix], buf[-max_prefix:]
