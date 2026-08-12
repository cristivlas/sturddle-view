"""Shared HTTP error parsing for LLM providers.

Anthropic/Ollama wrap errors in `{"error": {"message": "..."}}`; Gemini
wraps that same object in a single-element list. Pull the readable
message out so the RuntimeError (and the UI error_detail it feeds)
carries a clean string, not the raw JSON envelope.

Callers must pass the FULL body: parsing happens here, then the
extracted message is capped. Capping the body first would truncate the
JSON mid-structure (Gemini quota bodies run >500B) and defeat the parse.
"""
from __future__ import annotations

import json
import re

# Cap on the returned message. Keeps the RuntimeError / bus payload small
# even when a provider returns a wall of HTML or a verbose error. Applied
# to the extracted message, never to the body before parsing.
_DEFAULT_MAX_LEN = 500

_HTTP_BAD_REQUEST = 400
# Match the refusal, not the bare word: a 400 body often echoes the model id,
# and ids like "qwen3-thinking" would otherwise be read as a refusal. Missing
# a rephrase costs a generic error message; matching the wrong 400 puts a
# confident, wrong headline on it, so err toward the former.
_THINKING_UNSUPPORTED_RE = re.compile(
    r"(?:does\s+not|doesn'?t|cannot|can'?t)\s+support\s+thinking"
    r"|thinking\s+(?:is\s+)?not\s+(?:supported|enabled|available)",
    re.IGNORECASE,
)


class ThinkingUnsupported(RuntimeError):
    """A thinking-enabled request the model refused for lack of the
    capability.

    Subclasses RuntimeError so existing handlers keep catching it. The
    distinct class name is the contract with the UI: the AI done event
    reports `type(exc).__name__`, which the client maps to a deep link to
    the extended-thinking setting. Raise it with a message fit to show as
    is -- that is what the user reads.
    """


def is_thinking_unsupported(status_code: int, body: str) -> bool:
    """True when `body` reads as a bad-request rejection of thinking.

    Ask only on a request that actually enabled thinking -- that is the
    real signal, and this merely corroborates it, because the providers
    return no machine-readable code for the condition. Should the wording
    change, the caller reports a generic provider error rather than a
    wrong one.
    """
    return status_code == _HTTP_BAD_REQUEST and bool(_THINKING_UNSUPPORTED_RE.search(body))


def extract_error_message(body: str, *, max_len: int = _DEFAULT_MAX_LEN) -> str:
    """Return the most human-readable message from an error response,
    capped to `max_len` chars.

    Falls back to the raw body when the structure is missing or
    malformed (proxies, plain-text 502s, HTML pages).
    """
    return _extract(body)[:max_len]


def _extract(body: str) -> str:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body
    # Gemini wraps the error object in a list; unwrap so the dict path
    # below applies to all OpenAI-compat providers.
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str):
            return err
    return body
