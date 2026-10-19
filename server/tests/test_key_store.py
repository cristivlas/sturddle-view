"""Per-provider keyring storage. The conftest autouse fixture swaps in
an in-memory backend so the real OS keyring is never touched."""
from __future__ import annotations

import pytest

from sturddle_view import key_store
from sturddle_view.config import PROVIDER_ANTHROPIC, PROVIDER_GEMINI, PROVIDER_OLLAMA, Settings

ANTHROPIC_KEY = "sk-anthropic"
GEMINI_KEY = "tok-gemini"
ENV_KEY = "env-key"


def test_set_then_get_round_trips():
    assert key_store.set_api_key(PROVIDER_ANTHROPIC, ANTHROPIC_KEY) is True
    assert key_store.get_api_key(PROVIDER_ANTHROPIC) == ANTHROPIC_KEY


def test_unset_provider_returns_empty():
    assert key_store.get_api_key(PROVIDER_ANTHROPIC) == ""


def test_keys_isolated_per_provider():
    key_store.set_api_key(PROVIDER_ANTHROPIC, ANTHROPIC_KEY)
    key_store.set_api_key(PROVIDER_GEMINI, GEMINI_KEY)
    assert key_store.get_api_key(PROVIDER_ANTHROPIC) == ANTHROPIC_KEY
    assert key_store.get_api_key(PROVIDER_GEMINI) == GEMINI_KEY


def test_empty_value_clears():
    key_store.set_api_key(PROVIDER_ANTHROPIC, ANTHROPIC_KEY)
    key_store.set_api_key(PROVIDER_ANTHROPIC, "")
    assert key_store.get_api_key(PROVIDER_ANTHROPIC) == ""


def test_env_fallback_used_when_keyring_empty(monkeypatch):
    monkeypatch.setenv(key_store.ENV_FALLBACK, ENV_KEY)
    assert key_store.get_api_key(PROVIDER_ANTHROPIC) == ENV_KEY
    key_store.set_api_key(PROVIDER_ANTHROPIC, ANTHROPIC_KEY)
    assert key_store.get_api_key(PROVIDER_ANTHROPIC) == ANTHROPIC_KEY


def test_settings_property_dispatches_by_current_provider():
    s = Settings()
    s.ai_provider = PROVIDER_ANTHROPIC
    s.ai_api_key = ANTHROPIC_KEY
    s.ai_provider = PROVIDER_GEMINI
    s.ai_api_key = GEMINI_KEY
    s.ai_provider = PROVIDER_ANTHROPIC
    assert s.ai_api_key == ANTHROPIC_KEY
    s.ai_provider = PROVIDER_GEMINI
    assert s.ai_api_key == GEMINI_KEY


def test_keyless_provider_skips_keyring(monkeypatch):
    def fail(*_args):
        pytest.fail("keyring touched for keyless provider")
    monkeypatch.setattr(key_store, "get_api_key", fail)
    monkeypatch.setattr(key_store, "set_api_key", fail)
    s = Settings()
    s.ai_provider = PROVIDER_OLLAMA
    s.ai_api_key = ANTHROPIC_KEY
    assert s.ai_api_key == ""
