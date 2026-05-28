from __future__ import annotations

from sturddle_view.config import Settings


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings()
    s.human_side = "black"
    s.tc_initial_seconds = 600.0
    s.allow_takeback = False
    s.save_persisted(p)

    fresh = Settings()
    assert fresh.human_side == "white"  # default
    fresh.apply_persisted(p)
    assert fresh.human_side == "black"
    assert fresh.tc_initial_seconds == 600.0
    assert fresh.allow_takeback is False


def test_apply_persisted_missing_file_is_noop(tmp_path):
    s = Settings()
    s.human_side = "random"
    s.apply_persisted(tmp_path / "absent.json")
    assert s.human_side == "random"


def test_save_does_not_leak_secret_token(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings()
    original_token = s.token
    s.save_persisted(p)
    text = p.read_text()
    assert original_token not in text


def test_ai_models_per_provider_round_trip(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings()
    s.ai_provider = "ollama"
    s.ai_model = "gemma:latest"
    s.ai_provider = "anthropic"
    s.ai_model = "claude-opus-4-7"
    s.save_persisted(p)

    fresh = Settings()
    fresh.apply_persisted(p)
    assert fresh.ai_provider == "anthropic"
    assert fresh.ai_model == "claude-opus-4-7"
    fresh.ai_provider = "ollama"
    assert fresh.ai_model == "gemma:latest"
