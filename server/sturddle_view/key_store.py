"""Cross-platform secret storage for AI provider API keys.

Wraps the `keyring` library so the rest of the app uses a stable API
that works on macOS Keychain, Windows Credential Manager, and Linux
Secret Service. Falls back to the SV_AI_API_KEY environment variable
when no keyring backend is available (headless containers, server
mode without a desktop session).

Service name is fixed (`sturddle-view`); the account name encodes the
provider so multiple keys coexist -- `ai_api_key_<provider>`. Per
spec: UI never sees the full key back; GET surfaces a masked sentinel.

The manual-clear commands per OS are documented in
docs/ai-analysis-spec.md (Configuration section).
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


SERVICE_NAME = "sturddle-view"
ENV_FALLBACK = "SV_AI_API_KEY"


def _account(provider: str) -> str:
    return f"ai_api_key_{provider}"


def _keyring():
    """Return the keyring module if a real backend is available, else
    None. `keyring.backends.fail.Keyring` indicates no backend; treat
    that the same as a missing module."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except ImportError:
        return None
    try:
        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
    except Exception:
        return None
    return keyring


def get_api_key(provider: str) -> str:
    """Return the stored API key for `provider`, or empty string.

    Order: keyring -> SV_AI_API_KEY env var -> "". The env fallback is
    intentionally provider-agnostic for server-mode operators who set
    a single key out-of-band; per-provider env vars (SV_AI_API_KEY_<P>)
    are not supported because env-mode users typically run one provider
    at a time."""
    kr = _keyring()
    if kr is not None:
        try:
            val = kr.get_password(SERVICE_NAME, _account(provider))
            if val:
                return val
        except Exception as exc:
            log.warning("keyring read failed for %s: %s", provider, exc)
    return os.environ.get(ENV_FALLBACK, "")


def set_api_key(provider: str, value: str) -> bool:
    """Store the key for `provider`. Empty value deletes it. Returns
    True on success, False when no backend is available or the write
    failed -- caller can surface a UI warning so the user knows the
    key is session-only."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        if value:
            kr.set_password(SERVICE_NAME, _account(provider), value)
        else:
            try:
                kr.delete_password(SERVICE_NAME, _account(provider))
            except Exception:
                pass
        return True
    except Exception as exc:
        log.warning("keyring write failed for %s: %s", provider, exc)
        return False


def keyring_available() -> bool:
    """For diagnostics / startup logs only."""
    return _keyring() is not None
