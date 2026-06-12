"""Capability-driven thinking-mode resolution (Anthropic provider).

Covers: capability-tree parsing, the unreachable-API name heuristic,
cache seeding via list_models(), stream() wire shapes per mode, and the
/settings/ai/models thinking map.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.llm import anthropic as anthropic_mod
from sturddle_view.llm.anthropic import (
    THINKING_ADAPTIVE,
    THINKING_EXTENDED,
    THINKING_NONE,
    AnthropicProvider,
    _thinking_mode_fallback,
    _thinking_mode_from_capabilities,
)
from sturddle_view.llm.base import LLMProvider


@pytest.fixture(autouse=True)
def clear_thinking_cache():
    anthropic_mod._thinking_mode_cache.clear()
    yield
    anthropic_mod._thinking_mode_cache.clear()


def _caps(adaptive: bool, enabled: bool) -> dict:
    return {"capabilities": {"thinking": {"types": {
        "adaptive": {"supported": adaptive},
        "enabled": {"supported": enabled},
    }}}}


# ---------- Test fakes for httpx (GET + streaming POST) --------------


class _FakeGetResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _FakeSseResponse:
    def __init__(self) -> None:
        self.status_code = 200

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self) -> AsyncIterator[str]:
        yield 'data: {"type":"message_stop"}'


class _StreamCM:
    def __init__(self, resp: _FakeSseResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeSseResponse:
        return self._resp

    async def __aexit__(self, *args) -> None:
        return


class _FakeClient:
    """Answers GET /v1/models* and streaming POST /v1/messages."""

    def __init__(self, get_response: _FakeGetResponse) -> None:
        self._get_response = get_response
        self.get_calls = 0
        self.last_body: dict | None = None

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> None:
        return

    async def get(self, url, headers=None):
        self.get_calls += 1
        return self._get_response

    def stream(self, method, url, *, json=None, headers=None):
        self.last_body = json
        return _StreamCM(_FakeSseResponse())


@pytest.fixture
def install_fake_httpx(monkeypatch):
    def install(get_payload: dict, status: int = 200) -> _FakeClient:
        client = _FakeClient(_FakeGetResponse(status, get_payload))

        class _ShimHttpx:
            AsyncClient = lambda *a, **kw: client  # noqa: E731

        monkeypatch.setattr(anthropic_mod, "httpx", _ShimHttpx)
        return client

    return install


# ---------- Capability-tree parsing ----------------------------------


def test_capabilities_adaptive_wins_over_enabled():
    assert _thinking_mode_from_capabilities(_caps(True, True)) == THINKING_ADAPTIVE


def test_capabilities_enabled_only_is_extended():
    assert _thinking_mode_from_capabilities(_caps(False, True)) == THINKING_EXTENDED


def test_capabilities_neither_is_none_mode():
    assert _thinking_mode_from_capabilities(_caps(False, False)) == THINKING_NONE


def test_capabilities_tree_absent_returns_none():
    assert _thinking_mode_from_capabilities({"id": "m"}) is None
    assert _thinking_mode_from_capabilities({"capabilities": "bogus"}) is None


# ---------- Name heuristic (API-unreachable fallback) -----------------


@pytest.mark.parametrize("model,mode", [
    ("claude-fable-5", THINKING_ADAPTIVE),
    ("claude-mythos-5", THINKING_ADAPTIVE),
    ("claude-opus-4-6", THINKING_ADAPTIVE),
    ("claude-opus-4-8", THINKING_ADAPTIVE),
    ("claude-opus-4-5", THINKING_EXTENDED),
    ("claude-sonnet-4-6", THINKING_EXTENDED),
    ("", THINKING_EXTENDED),
])
def test_fallback_heuristic(model, mode):
    assert _thinking_mode_fallback(model) == mode


# ---------- Cache seeding via list_models -----------------------------


@pytest.mark.asyncio
async def test_list_models_seeds_thinking_modes(install_fake_httpx):
    install_fake_httpx({"data": [
        {"id": "claude-a", **_caps(True, False)},
        {"id": "claude-b", **_caps(False, True)},
        {"id": "claude-opus-4-8"},  # no capability tree -> heuristic
    ]})
    p = AnthropicProvider(api_key="k", model="claude-a")
    models = await p.list_models()
    assert models == ["claude-a", "claude-b", "claude-opus-4-8"]
    modes = p.thinking_modes(models)
    assert modes == {
        "claude-a": THINKING_ADAPTIVE,
        "claude-b": THINKING_EXTENDED,
        "claude-opus-4-8": THINKING_ADAPTIVE,
    }


# ---------- stream() wire shapes per mode -----------------------------


async def _drain(provider, **kw):
    async for _ in provider.stream("sys", [{"role": "user", "content": "x"}], **kw):
        pass


@pytest.mark.asyncio
async def test_stream_adaptive_shape_from_capability_fetch(install_fake_httpx):
    client = install_fake_httpx({"id": "claude-a", **_caps(True, False)})
    p = AnthropicProvider(api_key="k", model="claude-a", thinking_enabled=True,
                          thinking_budget_tokens=2048)
    await _drain(p)
    assert client.last_body["thinking"] == {"type": "adaptive"}
    assert client.last_body["max_tokens"] == anthropic_mod._DEFAULT_MAX_TOKENS
    # second stream serves the mode from cache -- no extra GET
    await _drain(p)
    assert client.get_calls == 1


@pytest.mark.asyncio
async def test_stream_extended_shape_lifts_max_tokens(install_fake_httpx):
    client = install_fake_httpx({"id": "claude-b", **_caps(False, True)})
    p = AnthropicProvider(api_key="k", model="claude-b", thinking_enabled=True,
                          thinking_budget_tokens=2048)
    await _drain(p)
    assert client.last_body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert client.last_body["max_tokens"] == anthropic_mod._DEFAULT_MAX_TOKENS + 2048


@pytest.mark.asyncio
async def test_stream_none_mode_omits_thinking(install_fake_httpx):
    client = install_fake_httpx({"id": "claude-c", **_caps(False, False)})
    p = AnthropicProvider(api_key="k", model="claude-c", thinking_enabled=True)
    await _drain(p)
    assert "thinking" not in client.last_body


@pytest.mark.asyncio
async def test_stream_fetch_failure_uses_heuristic_uncached(install_fake_httpx):
    client = install_fake_httpx({}, status=500)
    p = AnthropicProvider(api_key="k", model="claude-opus-4-8",
                          thinking_enabled=True)
    await _drain(p)
    assert client.last_body["thinking"] == {"type": "adaptive"}
    # fallback guesses must not be pinned
    assert anthropic_mod._thinking_mode_cache == {}


@pytest.mark.asyncio
async def test_stream_thinking_off_skips_resolution(install_fake_httpx):
    client = install_fake_httpx({}, status=500)
    p = AnthropicProvider(api_key="k", model="claude-a", thinking_enabled=False)
    await _drain(p)
    assert "thinking" not in client.last_body
    assert client.get_calls == 0


# ---------- /settings/ai/models thinking map --------------------------


class _StubProvider(LLMProvider):
    async def stream(self, *a, **kw):  # pragma: no cover -- not exercised
        raise NotImplementedError
        yield

    async def list_models(self) -> list[str]:
        return ["m1", "m2"]

    def thinking_modes(self, models: list[str]) -> dict[str, str]:
        return {m: THINKING_ADAPTIVE for m in models}


def test_models_endpoint_includes_thinking_map(tmp_path):
    settings = Settings(token="t", auth_disabled=True)
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    app.state.ai_provider_factory = lambda: _StubProvider()
    with TestClient(app) as c:
        r = c.get("/settings/ai/models")
        assert r.status_code == 200
        assert r.json() == {
            "models": ["m1", "m2"],
            "thinking": {"m1": "adaptive", "m2": "adaptive"},
        }
