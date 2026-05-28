"""Shared HTTP error parsing for LLM providers.

Both Anthropic and Ollama's OpenAI-compatible surface wrap errors in
`{"error": {"message": "..."}}`. Pull the readable message out so the
provider's RuntimeError carries a clean string -- which becomes the
`error_detail` field on the bus's terminal `ai_info` event, and lands
in toasts and the AI panel without users seeing the surrounding JSON.
"""
from __future__ import annotations

import json


def extract_error_message(body: str) -> str:
    """Return the most human-readable message from an error response.

    Falls back to the raw body when the structure is missing or
    malformed (proxies, plain-text 502s, HTML pages).
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str):
            return err
    return body
