"""Gemini provider wire-level tests (mocked HTTP).

GeminiProvider reuses openai_compat for translation + the SSE loop, so
those paths are covered by the ollama/openai_compat tests. Here we pin
the Gemini-specific surface: Bearer auth, the Google base URL + endpoint
paths, model-list `models/` prefix stripping, and the reasoning_effort
body field gated on thinking.

httpx is mocked at the module level. The control-plane (/models) call
uses gemini's own httpx; the streaming call uses openai_compat's -- each
is patched where the test exercises it, mirroring the ollama tests.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from sturddle_view.llm import gemini as gemini_mod
from sturddle_view.llm import openai_compat as openai_compat_mod
from sturddle_view.llm.gemini import DEFAULT_BASE_URL, GeminiProvider


# ---------- Fakes ----------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | str = "") -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict:
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("non-json body requested as json")

    @property
    def text(self) -> str:
        return str(self._body)


class _FakeGetClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_url: str | None = None
        self.last_headers: dict | None = None

    async def __aenter__(self) -> "_FakeGetClient":
        return self

    async def __aexit__(self, *args) -> None:
        return

    async def get(self, url: str, *, headers: dict | None = None) -> _FakeResponse:
        self.last_url = url
        self.last_headers = headers
        return self._response


def _install_get(monkeypatch, response: _FakeResponse) -> _FakeGetClient:
    client = _FakeGetClient(response)

    class _ShimHttpx:
        AsyncClient = lambda *a, **kw: client  # noqa: E731

    monkeypatch.setattr(gemini_mod, "httpx", _ShimHttpx)
    return client


# ---------- list_models ----------------------------------------------


# Native /v1beta/models URL the provider should hit (DEFAULT_BASE_URL
# minus the /openai compat suffix).
_NATIVE_MODELS_URL = DEFAULT_BASE_URL[: -len("/openai")] + "/models"


def _native_model(name: str, *, chat: bool = True) -> dict:
    methods = ["generateContent", "streamGenerateContent"] if chat else ["predict"]
    return {"name": name, "supportedGenerationMethods": methods}


@pytest.mark.asyncio
async def test_list_models_uses_native_endpoint_and_strips_prefix(monkeypatch):
    body = {
        "models": [
            _native_model("models/gemini-2.5-flash"),
            _native_model("models/gemini-2.5-pro"),
            _native_model("models/gemini-2.5-flash"),  # dup, must collapse
        ],
    }
    client = _install_get(monkeypatch, _FakeResponse(200, body))

    p = GeminiProvider(api_key="k-secret", model="m")
    models = await p.list_models()
    assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]
    # Listing hits the NATIVE endpoint (compat /openai/models omits the
    # capability metadata needed to filter).
    assert client.last_url == _NATIVE_MODELS_URL
    # Native endpoint takes x-goog-api-key, not Bearer (Bearer 401s here).
    assert client.last_headers["x-goog-api-key"] == "k-secret"
    assert "Authorization" not in client.last_headers


@pytest.mark.asyncio
async def test_list_models_filters_non_generate_content(monkeypatch):
    # Image/audio/live SKUs don't advertise generateContent and would 400
    # on the chat surface -- they must be dropped from the dropdown.
    body = {
        "models": [
            _native_model("models/gemini-2.5-flash", chat=True),
            _native_model("models/imagen-4.0", chat=False),       # predict-only
            _native_model("models/gemini-live-2.5", chat=False),  # live-only
        ],
    }
    _install_get(monkeypatch, _FakeResponse(200, body))
    p = GeminiProvider(api_key="k", model="m")
    assert await p.list_models() == ["gemini-2.5-flash"]


@pytest.mark.asyncio
async def test_list_models_without_key_raises(monkeypatch):
    p = GeminiProvider(api_key="", model="m")
    with pytest.raises(RuntimeError, match="API key"):
        await p.list_models()


@pytest.mark.asyncio
async def test_list_models_raises_on_http_error(monkeypatch):
    _install_get(monkeypatch, _FakeResponse(401, "bad key"))
    p = GeminiProvider(api_key="k", model="m")
    with pytest.raises(RuntimeError, match="401"):
        await p.list_models()


@pytest.mark.asyncio
async def test_list_models_handles_missing_models_field(monkeypatch):
    _install_get(monkeypatch, _FakeResponse(200, {}))
    p = GeminiProvider(api_key="k", model="m")
    assert await p.list_models() == []


# ---------- stream: request body shape -------------------------------


class _CapturingStreamClient:
    """Records the request body, then streams a single text chunk and a
    [DONE]. Class attributes capture across the instance the provider
    builds internally."""
    last_body: dict | None = None
    last_url: str | None = None
    last_headers: dict | None = None

    async def __aenter__(self) -> "_CapturingStreamClient":
        return self

    async def __aexit__(self, *args) -> None:
        return

    def stream(self, method, url, *, json=None, headers=None):
        type(self).last_body = json
        type(self).last_url = url
        type(self).last_headers = headers
        return _StreamCM()


class _StreamResponse:
    status_code = 200

    async def aiter_lines(self) -> AsyncIterator[str]:
        yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
        yield "data: [DONE]"


class _StreamCM:
    async def __aenter__(self) -> _StreamResponse:
        return _StreamResponse()

    async def __aexit__(self, *args) -> None:
        return


def _install_stream(monkeypatch) -> type[_CapturingStreamClient]:
    _CapturingStreamClient.last_body = None
    _CapturingStreamClient.last_url = None
    _CapturingStreamClient.last_headers = None

    class _ShimHttpx:
        AsyncClient = lambda *a, **kw: _CapturingStreamClient()  # noqa: E731

    monkeypatch.setattr(openai_compat_mod, "httpx", _ShimHttpx)
    return _CapturingStreamClient


async def _drain(provider, **kw) -> list:
    return [c async for c in provider.stream(system="SYS", messages=[], **kw)]


@pytest.mark.asyncio
async def test_stream_posts_to_compat_endpoint_with_bearer(monkeypatch):
    cls = _install_stream(monkeypatch)
    p = GeminiProvider(api_key="k", model="gemini-2.5-flash")
    chunks = await _drain(p)
    assert [c.text for c in chunks if c.kind == "text"] == ["hi"]
    assert cls.last_url == f"{DEFAULT_BASE_URL}/chat/completions"
    assert cls.last_headers["Authorization"] == "Bearer k"
    assert cls.last_body["model"] == "gemini-2.5-flash"
    assert cls.last_body["stream"] is True
    # System prompt is folded into a leading system message.
    assert cls.last_body["messages"][0] == {"role": "system", "content": "SYS"}


@pytest.mark.asyncio
async def test_stream_without_key_raises(monkeypatch):
    p = GeminiProvider(api_key="", model="m")
    with pytest.raises(RuntimeError, match="API key"):
        await _drain(p)


@pytest.mark.asyncio
async def test_thinking_off_omits_reasoning_effort(monkeypatch):
    cls = _install_stream(monkeypatch)
    p = GeminiProvider(api_key="k", model="m", thinking_enabled=False)
    await _drain(p)
    assert "reasoning_effort" not in cls.last_body


@pytest.mark.asyncio
async def test_thinking_enabled_sets_reasoning_effort(monkeypatch):
    cls = _install_stream(monkeypatch)
    p = GeminiProvider(api_key="k", model="m", thinking_enabled=True)
    await _drain(p)
    assert cls.last_body["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_thinking_false_override_forces_off(monkeypatch):
    # Verifier sub-runs pass thinking=False to suppress reasoning even
    # when the provider has thinking enabled.
    cls = _install_stream(monkeypatch)
    p = GeminiProvider(api_key="k", model="m", thinking_enabled=True)
    await _drain(p, thinking=False)
    assert "reasoning_effort" not in cls.last_body


@pytest.mark.asyncio
async def test_custom_base_url_trailing_slash_trimmed(monkeypatch):
    cls = _install_stream(monkeypatch)
    p = GeminiProvider(api_key="k", model="m", base_url="http://proxy/v1beta/openai/")
    await _drain(p)
    assert cls.last_url == "http://proxy/v1beta/openai/chat/completions"
