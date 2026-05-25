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
from sturddle_view.llm._errors import extract_error_message
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


def test_put_ai_model_evicts_previous_ollama_model(tmp_path, monkeypatch):
    """Switching models triggers eviction of the previous one. Without
    this, the Ollama daemon keeps the old model in VRAM and the new
    one fails to load with 'resource limits'."""
    evicted = []

    async def _fake_evict(self, model):
        evicted.append(model)

    monkeypatch.setattr(OllamaProvider, "evict_model", _fake_evict)

    with _client_for_provider(tmp_path, provider="ollama", base_url="http://fake") as c:
        # Seed the saved model.
        c.put("/settings", json={"ai_model": "old:latest"})
        evicted.clear()
        # User switches model.
        r = c.put("/settings", json={"ai_model": "new:latest"})
        assert r.status_code == 200, r.text
        assert evicted == ["old:latest"]


def test_put_ai_provider_off_ollama_evicts_current_model(tmp_path, monkeypatch):
    evicted = []

    async def _fake_evict(self, model):
        evicted.append(model)

    monkeypatch.setattr(OllamaProvider, "evict_model", _fake_evict)

    with _client_for_provider(tmp_path, provider="ollama", base_url="http://fake") as c:
        c.put("/settings", json={"ai_model": "loaded:latest"})
        evicted.clear()
        # Switch provider away from Ollama.
        r = c.put("/settings", json={"ai_provider": "anthropic"})
        assert r.status_code == 200, r.text
        assert evicted == ["loaded:latest"]


def test_put_unrelated_setting_does_not_evict(tmp_path, monkeypatch):
    evicted = []

    async def _fake_evict(self, model):
        evicted.append(model)

    monkeypatch.setattr(OllamaProvider, "evict_model", _fake_evict)

    with _client_for_provider(tmp_path, provider="ollama", base_url="http://fake") as c:
        c.put("/settings", json={"ai_model": "stay:latest"})
        evicted.clear()
        # Touch an unrelated setting -- no eviction.
        r = c.put("/settings", json={"view_show_pgn_comments": True})
        assert r.status_code == 200, r.text
        assert evicted == []


# ---------- extract_error_message: cross-provider error parsing -----


def test_extract_error_message_ollama_openai_shape():
    body = '{"error":{"message":"model does not support tools","type":"invalid_request_error"}}'
    assert extract_error_message(body) == "model does not support tools"


def test_extract_error_message_anthropic_shape():
    # Anthropic wraps in {"type": "error", "error": {"type": "...", "message": "..."}}
    body = '{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'
    assert extract_error_message(body) == "invalid x-api-key"


def test_extract_error_message_string_error_field():
    # Some upstreams use a bare string instead of a dict. Don't fail
    # on the shape we didn't expect; surface what's there.
    body = '{"error":"rate limited, try again"}'
    assert extract_error_message(body) == "rate limited, try again"


def test_extract_error_message_non_json_fallback():
    # A misconfigured proxy returns plain text / HTML. The raw body
    # is returned verbatim so the user still sees something.
    assert extract_error_message("Bad Gateway") == "Bad Gateway"
    assert extract_error_message("<html>nginx</html>") == "<html>nginx</html>"


def test_extract_error_message_missing_error_field_fallback():
    # Valid JSON, no `error` key -- return raw so we don't silently
    # swallow whatever the upstream said.
    body = '{"status":"degraded","retry_after":30}'
    assert extract_error_message(body) == body


@pytest.mark.asyncio
async def test_ollama_evict_model_posts_keep_alive_zero(monkeypatch):
    """When the user switches models, settings.py calls evict_model on
    the previous one. The daemon owns lifecycle; our request just tells
    it to drop the model. keep_alive=0 is the documented signal."""
    # Reuse the GET-style fake (evict_model uses POST but we only care
    # the URL hits /api/generate with the right body); add a minimal
    # post() that records what we sent.
    class _RecordingClient:
        def __init__(self):
            self.last_post_url = None
            self.last_post_json = None
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return
        async def post(self, url, *, json=None):
            self.last_post_url = url
            self.last_post_json = json
            return _FakeResponse(200, {})

    rec = _RecordingClient()

    class _ShimHttpx:
        AsyncClient = lambda *a, **kw: rec  # noqa: E731

    monkeypatch.setattr(ollama_mod, "httpx", _ShimHttpx)

    provider = OllamaProvider(base_url="http://fake", model="x")
    await provider.evict_model("gemma2:latest")

    assert rec.last_post_url == "http://fake/api/generate"
    assert rec.last_post_json == {"model": "gemma2:latest", "keep_alive": 0}


@pytest.mark.asyncio
async def test_ollama_evict_model_empty_name_is_noop():
    # Calling with "" must not hit the network -- caller has nothing to
    # evict. No fake httpx installed; if a request was attempted, the
    # real httpx would try to connect.
    provider = OllamaProvider(base_url="http://nonexistent.invalid", model="x")
    await provider.evict_model("")  # must not raise


@pytest.mark.asyncio
async def test_ollama_stream_error_message_is_extracted_not_wrapped(monkeypatch):
    """When Ollama returns the OpenAI error envelope, the RuntimeError
    we raise must carry the inner `message`, not the wrapping JSON.
    Without this, toasts on the client showed the full JSON body."""
    body = '{"error":{"message":"model does not support tools","type":"invalid_request_error"}}'

    # Stub httpx for the streaming call -- we only need the
    # non-200 path here, so the stream body is empty.
    class _FakeStreamResponse:
        status_code = 400
        async def aread(self) -> bytes:
            return body.encode("utf-8")

    class _StreamCM:
        async def __aenter__(self): return _FakeStreamResponse()
        async def __aexit__(self, *a): return

    class _FakeStreamClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return
        def stream(self, *a, **kw): return _StreamCM()

    class _ShimHttpx:
        AsyncClient = lambda *a, **kw: _FakeStreamClient()  # noqa: E731

    monkeypatch.setattr(ollama_mod, "httpx", _ShimHttpx)

    provider = OllamaProvider(base_url="http://fake", model="m")
    with pytest.raises(RuntimeError) as ei:
        async for _ in provider.stream(system="", messages=[]):
            pass
    msg = str(ei.value)
    assert "does not support tools" in msg
    # The raw JSON envelope must NOT appear -- that was the bug.
    assert "invalid_request_error" not in msg
