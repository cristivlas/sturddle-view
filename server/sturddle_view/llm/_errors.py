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

# Cap on the returned message. Keeps the RuntimeError / bus payload small
# even when a provider returns a wall of HTML or a verbose error. Applied
# to the extracted message, never to the body before parsing.
_DEFAULT_MAX_LEN = 500


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
