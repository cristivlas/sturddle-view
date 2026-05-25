"""Provider list_models() + /settings/ai/models endpoint.

Mocks httpx at the module level for each provider, then exercises the
endpoint end-to-end via TestClient so the wiring through the provider
factory is covered too.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.llm import anthropic as anthropic_mod
from sturddle_view.llm import ollama as ollama_mod
from sturddle_view.llm.anthropic import AnthropicProvider
from sturddle_view.llm.ollama import OllamaProvider


# ---------- Test fakes for httpx -------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | str) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("non-json body requested as json")

    @property
    def text(self) -> str:
        return str(self._body)


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_get_url: str | None = None
        self.last_get_headers: dict | None = None

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> None:
        return

    async def get(self, url: str, *, headers: dict | None = None) -> _FakeResponse:
        self.last_get_url = url
        self.last_get_headers = headers
        return self._response


def _install_fake_httpx(monkeypatch, module, response: _FakeResponse) -> _FakeClient:
    client = _FakeClient(response)

    class _ShimHttpx:
        AsyncClient = lambda *a, **kw: client  # noqa: E731

    monkeypatch.setattr(module, "httpx", _ShimHttpx)
    return client


# ---------- Ollama ---------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_list_models_returns_sorted_unique_ids(monkeypatch):
    body = {
        "object": "list",
        "data": [
            {"id": "gemma2:latest"},
            {"id": "llama3:8b"},
            {"id": "gemma2:latest"},  # dup, must collapse
        ],
    }
    client = _install_fake_httpx(monkeypatch, ollama_mod, _FakeResponse(200, body))

    p = OllamaProvider(base_url="http://fake", model="m")
    models = await p.list_models()
    assert models == ["gemma2:latest", "llama3:8b"]
    assert client.last_get_url == "http://fake/v1/models"


@pytest.mark.asyncio
async def test_ollama_list_models_handles_missing_data_field(monkeypatch):
    _install_fake_httpx(monkeypatch, ollama_mod, _FakeResponse(200, {}))
    p = OllamaProvider(base_url="http://fake", model="m")
    assert await p.list_models() == []


@pytest.mark.asyncio
async def test_ollama_list_models_raises_on_http_error(monkeypatch):
    _install_fake_httpx(monkeypatch, ollama_mod, _FakeResponse(503, "down"))
    p = OllamaProvider(base_url="http://fake", model="m")
    with pytest.raises(RuntimeError, match="503"):
        await p.list_models()


# ---------- Anthropic ------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_list_models_sends_auth_headers(monkeypatch):
    body = {
        "data": [
            {"id": "claude-opus-4-5", "display_name": "Claude Opus 4.5"},
            {"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"},
        ],
    }
    client = _install_fake_httpx(monkeypatch, anthropic_mod, _FakeResponse(200, body))

    p = AnthropicProvider(api_key="sk-test", model="m")
    models = await p.list_models()
    assert "claude-opus-4-5" in models
    assert "claude-sonnet-4-5" in models
    # Headers shape the API requires:
    assert client.last_get_headers["x-api-key"] == "sk-test"
    assert "anthropic-version" in client.last_get_headers


@pytest.mark.asyncio
async def test_anthropic_list_models_without_key_raises(monkeypatch):
    p = AnthropicProvider(api_key="", model="m")
    with pytest.raises(RuntimeError, match="API key"):
        await p.list_models()


@pytest.mark.asyncio
async def test_anthropic_list_models_raises_on_auth_failure(monkeypatch):
    _install_fake_httpx(monkeypatch, anthropic_mod, _FakeResponse(401, "bad key"))
    p = AnthropicProvider(api_key="sk-bad", model="m")
    with pytest.raises(RuntimeError, match="401"):
        await p.list_models()


# ---------- /settings/ai/models endpoint -----------------------------


def _client_for_provider(tmp_path, *, provider: str, api_key: str = "", base_url: str = "") -> TestClient:
    settings = Settings(token="t", auth_disabled=True)
    settings.ai_enabled = True
    settings.ai_provider = provider
    settings.ai_api_key = api_key
    settings.ai_base_url = base_url
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    return TestClient(app)


def test_endpoint_returns_models_for_ollama(tmp_path, monkeypatch):
    body = {"data": [{"id": "gemma2:latest"}, {"id": "llama3:8b"}]}
    _install_fake_httpx(monkeypatch, ollama_mod, _FakeResponse(200, body))

    with _client_for_provider(tmp_path, provider="ollama", base_url="http://fake") as c:
        r = c.get("/settings/ai/models")
        assert r.status_code == 200, r.text
        assert sorted(r.json()["models"]) == ["gemma2:latest", "llama3:8b"]


def test_endpoint_returns_5xx_when_provider_unreachable(tmp_path, monkeypatch):
    _install_fake_httpx(monkeypatch, ollama_mod, _FakeResponse(503, "down"))

    with _client_for_provider(tmp_path, provider="ollama", base_url="http://fake") as c:
        r = c.get("/settings/ai/models")
        assert r.status_code == 502, r.text
        assert "503" in r.json()["detail"]


def test_endpoint_returns_5xx_for_anthropic_stub_without_models(tmp_path):
    # AnthropicProvider built without an API key raises -- the endpoint
    # surfaces that to the UI rather than returning an empty list.
    with _client_for_provider(tmp_path, provider="anthropic", api_key="") as c:
        r = c.get("/settings/ai/models")
        assert r.status_code == 502, r.text
        assert "API key" in r.json()["detail"]
