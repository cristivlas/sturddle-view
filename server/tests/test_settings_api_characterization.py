"""Characterization tests for PUT /settings.

Pins the observable behavior (status code + persisted value) of every
field update_settings touches, so the handler can be refactored from its
if-cascade into a dispatch table without silent behavior drift.

Assertions are on status + round-tripped value, not exact error wording,
except where a 400 message is the only observable (then: field name is in
detail). analysis_engine_id is new and exercised against the target
behavior (round-trip + unknown-id reject).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry


@pytest.fixture
def client(tmp_path):
    settings = Settings(token="t", auth_disabled=True)
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_with_engine(tmp_path):
    """Client whose registry has one engine; yields (client, engine_id)."""
    settings = Settings(token="t", auth_disabled=True)
    registry = EngineRegistry(path=tmp_path / "engines.json")
    e = registry.add(name="MyEngine", path="/nonexistent/engine")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c, e.id


def _put(client, body: dict):
    return client.put("/settings", json=body)


def _get(client) -> dict:
    return client.get("/settings").json()


# --- enum fields: accept valid, reject invalid (field name in 400) -------

# (field, valid_value) pairs -- the value each enum field should accept.
_ENUM_VALID = [
    ("human_side", "black"),
    ("board_style", "blue"),
    ("play_eval_pov", "human"),
    ("ribbon_side", "left"),
    ("engine_default_book_order", "random"),
]
# (field, invalid_value) pairs -- a value each enum field should reject.
_ENUM_INVALID = [
    ("human_side", "sideways"),
    ("board_style", "polka-dot"),
    ("play_eval_pov", "diagonal"),
    ("ribbon_side", "top"),
    ("engine_default_book_order", "shuffle"),
]


@pytest.mark.parametrize("field,valid", _ENUM_VALID)
def test_enum_field_accepts_valid(client, field, valid):
    r = _put(client, {field: valid})
    assert r.status_code == 200, r.text
    assert _get(client)[field] == valid


@pytest.mark.parametrize("field,invalid", _ENUM_INVALID)
def test_enum_field_rejects_invalid(client, field, invalid):
    r = _put(client, {field: invalid})
    assert r.status_code == 400
    assert field in r.json()["detail"]


# --- boolean fields: truthy coercion round-trips -------------------------

@pytest.mark.parametrize("field", [
    "pgn_autosave",
    "allow_takeback",
    "auto_claim_draws",
    "inherit_pgn_clocks",
    "play_show_eval_graph",
    "view_show_pgn_comments",
    "ai_enabled",
    "ai_thinking_enabled",
])
def test_boolean_field_round_trips(client, field):
    assert _put(client, {field: True}).status_code == 200
    assert _get(client)[field] is True
    assert _put(client, {field: False}).status_code == 200
    assert _get(client)[field] is False


# --- AI provider enum ----------------------------------------------------

def test_ai_provider_accepts_valid(client):
    r = _put(client, {"ai_provider": "gemini"})
    assert r.status_code == 200, r.text
    assert _get(client)["ai_provider"] == "gemini"


def test_ai_provider_rejects_invalid(client):
    r = _put(client, {"ai_provider": "skynet"})
    assert r.status_code == 400
    assert "ai_provider" in r.json()["detail"]


# --- AI string fields ----------------------------------------------------

def test_ai_base_url_round_trips(client):
    r = _put(client, {"ai_base_url": "http://localhost:11434"})
    assert r.status_code == 200
    assert _get(client)["ai_base_url"] == "http://localhost:11434"


def test_ai_base_url_strips_whitespace(client):
    _put(client, {"ai_base_url": "  http://x:1  "})
    assert _get(client)["ai_base_url"] == "http://x:1"


# --- AI thinking budget: floor enforced ----------------------------------

def test_ai_thinking_budget_at_floor_accepted(client):
    r = _put(client, {"ai_thinking_budget_tokens": 1024})
    assert r.status_code == 200
    assert _get(client)["ai_thinking_budget_tokens"] == 1024


def test_ai_thinking_budget_below_floor_rejected(client):
    r = _put(client, {"ai_thinking_budget_tokens": 1023})
    assert r.status_code == 400
    assert "ai_thinking_budget_tokens" in r.json()["detail"]


def test_ai_thinking_budget_non_integer_rejected(client):
    r = _put(client, {"ai_thinking_budget_tokens": "lots"})
    assert r.status_code == 400


# --- AI depth caps: floor enforced ---------------------------------------

@pytest.mark.parametrize("field", ["ai_analyze_max_depth", "ai_verification_depth"])
def test_ai_depth_at_floor_accepted(client, field):
    r = _put(client, {field: 1})
    assert r.status_code == 200
    assert _get(client)[field] == 1


@pytest.mark.parametrize("field", ["ai_analyze_max_depth", "ai_verification_depth"])
def test_ai_depth_below_floor_rejected(client, field):
    r = _put(client, {field: 0})
    assert r.status_code == 400
    assert field in r.json()["detail"]


@pytest.mark.parametrize("field", ["ai_analyze_max_depth", "ai_verification_depth"])
def test_ai_depth_non_integer_rejected(client, field):
    r = _put(client, {field: "deep"})
    assert r.status_code == 400


# --- AI API key: mask sentinel = no-change; real value sets --------------

def test_ai_api_key_real_value_sets(client):
    r = _put(client, {"ai_api_key": "sk-secret"})
    assert r.status_code == 200
    # GET never echoes the real key; it reports set-ness + mask.
    body = _get(client)
    assert body["ai_api_key_set"] is True
    assert body["ai_api_key"] == "***"


def test_ai_api_key_mask_is_no_change(client):
    _put(client, {"ai_api_key": "sk-secret"})
    # Echoing the mask back must not clear the stored key.
    r = _put(client, {"ai_api_key": "***"})
    assert r.status_code == 200
    assert _get(client)["ai_api_key_set"] is True


def test_ai_api_key_empty_clears(client):
    _put(client, {"ai_api_key": "sk-secret"})
    _put(client, {"ai_api_key": ""})
    assert _get(client)["ai_api_key_set"] is False


# --- partial payload leaves unrelated fields untouched -------------------

def test_partial_update_preserves_other_fields(client):
    _put(client, {"human_side": "black", "ai_provider": "gemini"})
    _put(client, {"board_style": "blue"})
    body = _get(client)
    assert body["human_side"] == "black"
    assert body["ai_provider"] == "gemini"
    assert body["board_style"] == "blue"


# --- analysis_engine_id (new) --------------------------------------------

def test_analysis_engine_id_round_trips(client_with_engine):
    client, engine_id = client_with_engine
    r = _put(client, {"analysis_engine_id": engine_id})
    assert r.status_code == 200, r.text
    assert _get(client)["analysis_engine_id"] == engine_id


def test_analysis_engine_id_empty_clears(client_with_engine):
    client, engine_id = client_with_engine
    _put(client, {"analysis_engine_id": engine_id})
    r = _put(client, {"analysis_engine_id": ""})
    assert r.status_code == 200
    assert _get(client)["analysis_engine_id"] == ""


def test_analysis_engine_id_unknown_rejected(client_with_engine):
    client, _engine_id = client_with_engine
    r = _put(client, {"analysis_engine_id": "does-not-exist"})
    assert r.status_code == 400
    assert "analysis_engine_id" in r.json()["detail"]