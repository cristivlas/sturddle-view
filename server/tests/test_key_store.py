"""Per-provider keyring storage. The conftest autouse fixture swaps in
an in-memory backend so the real OS keyring is never touched."""
from __future__ import annotations

from sturddle_view import key_store
from sturddle_view.config import Settings


def test_set_then_get_round_trips():
    assert key_store.set_api_key("anthropic", "sk-abc") is True
    assert key_store.get_api_key("anthropic") == "sk-abc"


def test_unset_provider_returns_empty():
    assert key_store.get_api_key("anthropic") == ""


def test_keys_isolated_per_provider():
    key_store.set_api_key("anthropic", "sk-abc")
    key_store.set_api_key("ollama", "ollama-tok")
    assert key_store.get_api_key("anthropic") == "sk-abc"
    assert key_store.get_api_key("ollama") == "ollama-tok"


def test_empty_value_clears():
    key_store.set_api_key("anthropic", "sk-abc")
    key_store.set_api_key("anthropic", "")
    assert key_store.get_api_key("anthropic") == ""


def test_env_fallback_used_when_keyring_empty(monkeypatch):
    monkeypatch.setenv(key_store.ENV_FALLBACK, "env-key")
    assert key_store.get_api_key("anthropic") == "env-key"
    key_store.set_api_key("anthropic", "stored")
    assert key_store.get_api_key("anthropic") == "stored"


def test_settings_property_dispatches_by_current_provider():
    s = Settings()
    s.ai_provider = "anthropic"
    s.ai_api_key = "sk-anthropic"
    s.ai_provider = "ollama"
    s.ai_api_key = "tok-ollama"
    s.ai_provider = "anthropic"
    assert s.ai_api_key == "sk-anthropic"
    s.ai_provider = "ollama"
    assert s.ai_api_key == "tok-ollama"
