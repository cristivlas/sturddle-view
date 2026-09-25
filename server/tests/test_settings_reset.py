"""POST /settings/reset: the settings file returns to defaults, while
registered engines and API keys (stored elsewhere) survive."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import sturddle_view.api.settings as settings_api
import sturddle_view.config as cfg
from sturddle_view.app import create_app
from sturddle_view.config import (
    AI_API_KEY_KEY,
    AI_MAX_TOOL_ROUNDS_KEY,
    BOARD_STYLE_KEY,
    ENGINE_THREADS_KEY,
    HVE_DIFFICULTY_KEY,
    PLAYER_NAME_KEY,
    TC_INITIAL_KEY,
    Settings,
)
from sturddle_view.engines import EngineRegistry
from .conftest import REGISTRY_FILE

SETTINGS_PATH = "/settings"
RESET_PATH = "/settings/reset"
ENGINES_NO_PROBE_PATH = "/engines?probe=false"
AI_API_KEY_SET_KEY = "ai_api_key_set"
SELECTED_ID_KEY = "selected_id"
TOKEN = "t"
ENGINE_NAME = "MyEngine"
ENGINE_FILE = "my-engine"
API_KEY = "sk-test"
PLAYER_NAME = "Alyssa"
ENGINE_THREADS = 4
ENV_HVE_DIFFICULTY = "SV_HVE_DIFFICULTY"
ENV_HVE_DIFFICULTY_VALUE = 3
# Non-default values spanning the play, display, engine and AI groups.
NON_DEFAULTS = {
    TC_INITIAL_KEY: 60.0,
    BOARD_STYLE_KEY: "green",
    PLAYER_NAME_KEY: PLAYER_NAME,
    HVE_DIFFICULTY_KEY: 5,
    ENGINE_THREADS_KEY: ENGINE_THREADS,
    AI_MAX_TOOL_ROUNDS_KEY: 50,
}


class _FakeHve:
    def __init__(self):
        self.respawns = 0

    async def apply_engine_settings_live(self):
        self.respawns += 1


def _app(registry):
    return create_app(settings=Settings(token=TOKEN, auth_disabled=True), engine_registry=registry)


@pytest.fixture
def registry(tmp_path):
    return EngineRegistry(path=tmp_path / REGISTRY_FILE)


@pytest.fixture
def client(registry):
    with TestClient(_app(registry)) as c:
        yield c


@pytest.fixture
def fake_hve(monkeypatch):
    hve = _FakeHve()
    monkeypatch.setattr(settings_api, "live_hve", lambda state: hve)
    return hve


def _put(client, patch):
    assert client.put(SETTINGS_PATH, json=patch).status_code == 200


def test_reset_restores_defaults(client):
    defaults = client.get(SETTINGS_PATH).json()
    _put(client, NON_DEFAULTS)
    r = client.post(RESET_PATH)
    assert r.status_code == 200
    assert r.json() == defaults
    assert client.get(SETTINGS_PATH).json() == defaults


def test_reset_persists_defaults(client):
    _put(client, NON_DEFAULTS)
    client.post(RESET_PATH)
    saved = json.loads(cfg.default_settings_file().read_text(encoding="utf-8"))
    fresh = Settings()
    for key in NON_DEFAULTS:
        assert saved[key] == getattr(fresh, key)


def test_reset_keeps_api_key(client):
    _put(client, {AI_API_KEY_KEY: API_KEY})
    client.post(RESET_PATH)
    assert client.get(SETTINGS_PATH).json()[AI_API_KEY_SET_KEY] is True


def test_reset_keeps_registered_engines(client, registry, tmp_path):
    engine = registry.add(name=ENGINE_NAME, path=str(tmp_path / ENGINE_FILE))
    registry.select(engine.id)
    before = client.get(ENGINES_NO_PROBE_PATH).json()
    assert before[SELECTED_ID_KEY] == engine.id
    client.post(RESET_PATH)
    assert client.get(ENGINES_NO_PROBE_PATH).json() == before


def test_reset_keeps_env_overrides(monkeypatch, registry):
    monkeypatch.setenv(ENV_HVE_DIFFICULTY, str(ENV_HVE_DIFFICULTY_VALUE))
    with TestClient(_app(registry)) as c:
        _put(c, {HVE_DIFFICULTY_KEY: NON_DEFAULTS[HVE_DIFFICULTY_KEY]})
        assert c.post(RESET_PATH).json()[HVE_DIFFICULTY_KEY] == ENV_HVE_DIFFICULTY_VALUE


def test_reset_respawns_engine_when_engine_defaults_change(client, fake_hve):
    _put(client, {ENGINE_THREADS_KEY: ENGINE_THREADS})
    fake_hve.respawns = 0
    client.post(RESET_PATH)
    assert fake_hve.respawns == 1


def test_reset_skips_respawn_when_engine_defaults_unchanged(client, fake_hve):
    _put(client, {PLAYER_NAME_KEY: PLAYER_NAME})
    client.post(RESET_PATH)
    assert fake_hve.respawns == 0
