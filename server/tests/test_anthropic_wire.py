"""Anthropic provider wire-level tests.

Same shim approach as test_ollama_wire: stub httpx so we drive the SSE
parser deterministically. Covers:
- happy text streaming -> text chunks
- tool_use accumulation across input_json_delta + emission at stop
- thinking_delta -> thinking chunk
- transcript captures request body + every raw line
- HTTP non-200 raises with the upstream `detail` message
- malformed tool args raise instead of silently coercing to {}
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from sturddle_view.llm import Transcript
from sturddle_view.llm import anthropic as anthropic_mod
from sturddle_view.llm.anthropic import AnthropicProvider


# ---------- Test fakes for httpx -------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, lines: list[str], body: bytes = b"") -> None:
        self.status_code = status_code
        self._lines = lines
        self._body = body

    async def aread(self) -> bytes:
        return self._body

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_body: dict | None = None
        self.last_headers: dict | None = None

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> None:
        return

    def stream(self, method, url, *, json=None, headers=None):
        self.last_body = json
        self.last_headers = headers
        return _StreamCM(self._response)


class _StreamCM:
    def __init__(self, resp: _FakeResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeResponse:
        return self._resp

    async def __aexit__(self, *args) -> None:
        return


@pytest.fixture
def install_fake_httpx(monkeypatch):
    holder: dict[str, _FakeClient | None] = {"client": None}

    def install(lines: list[str], status: int = 200, body: bytes = b"") -> None:
        resp = _FakeResponse(status_code=status, lines=lines, body=body)
        client = _FakeClient(resp)
        holder["client"] = client

        class _ShimHttpx:
            AsyncClient = lambda *a, **kw: client  # noqa: E731

        monkeypatch.setattr(anthropic_mod, "httpx", _ShimHttpx)

    install.holder = holder  # type: ignore[attr-defined]
    return install


# ---------- Tests ----------------------------------------------------


@pytest.mark.asyncio
async def test_text_streaming_emits_text_chunks(install_fake_httpx):
    install_fake_httpx(lines=[
        'data: {"type":"message_start"}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_stop"}',
    ])
    provider = AnthropicProvider(api_key="sk-test", model="claude-test")
    chunks = []
    async for c in provider.stream(system="SYS", messages=[{"role": "user", "content": "hi"}]):
        chunks.append(c)

    assert [c.kind for c in chunks] == ["text", "text"]
    assert "".join(c.text for c in chunks) == "Hello world"


@pytest.mark.asyncio
async def test_tool_use_accumulates_and_emits_on_block_stop(install_fake_httpx):
    install_fake_httpx(lines=[
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tu_1","name":"analyze"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"fen\\""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":":\\"startpos\\",\\"depth\\":12}"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_stop"}',
    ])
    provider = AnthropicProvider(api_key="sk-test", model="m")
    chunks = []
    async for c in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
        chunks.append(c)

    assert len(chunks) == 1
    tc = chunks[0]
    assert tc.kind == "tool_use"
    assert tc.tool_use_id == "tu_1"
    assert tc.tool_name == "analyze"
    assert tc.tool_input == {"fen": "startpos", "depth": 12}


@pytest.mark.asyncio
async def test_thinking_delta_emits_thinking_chunk(install_fake_httpx):
    install_fake_httpx(lines=[
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"reasoning..."}}',
        'data: {"type":"content_block_stop","index":0}',
    ])
    provider = AnthropicProvider(api_key="sk-test", model="m")
    chunks = []
    async for c in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
        chunks.append(c)
    assert [c.kind for c in chunks] == ["thinking"]
    assert chunks[0].text == "reasoning..."


@pytest.mark.asyncio
async def test_text_and_tool_use_in_one_response(install_fake_httpx):
    install_fake_httpx(lines=[
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"let me check"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu_2","name":"analyze"}}',
        'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{}"}}',
        'data: {"type":"content_block_stop","index":1}',
    ])
    provider = AnthropicProvider(api_key="sk-test", model="m")
    chunks = []
    async for c in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
        chunks.append(c)
    assert [c.kind for c in chunks] == ["text", "tool_use"]
    assert chunks[0].text == "let me check"
    assert chunks[1].tool_name == "analyze"


@pytest.mark.asyncio
async def test_request_body_and_headers_set(install_fake_httpx):
    install_fake_httpx(lines=['data: {"type":"message_stop"}'])
    provider = AnthropicProvider(api_key="sk-secret", model="claude-1")
    chunks = []
    async for c in provider.stream(
        system="SYS",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "t", "description": "d", "input_schema": {}}],
    ):
        chunks.append(c)
    client = install_fake_httpx.holder["client"]
    assert client.last_body["model"] == "claude-1"
    assert client.last_body["system"] == "SYS"
    assert client.last_body["stream"] is True
    assert client.last_body["tools"][0]["name"] == "t"
    assert client.last_headers["x-api-key"] == "sk-secret"


@pytest.mark.asyncio
async def test_http_error_includes_upstream_message(install_fake_httpx):
    install_fake_httpx(
        lines=[],
        status=401,
        body=b'{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}',
    )
    provider = AnthropicProvider(api_key="sk-bad", model="m")
    with pytest.raises(RuntimeError, match="invalid x-api-key"):
        async for _ in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
            pass


@pytest.mark.asyncio
async def test_malformed_tool_args_raises_runtimeerror(install_fake_httpx):
    install_fake_httpx(lines=[
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tu","name":"analyze"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{not json"}}',
        'data: {"type":"content_block_stop","index":0}',
    ])
    provider = AnthropicProvider(api_key="sk-test", model="m")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        async for _ in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
            pass


@pytest.mark.asyncio
async def test_transcript_captures_request_and_wire(install_fake_httpx, tmp_path):
    install_fake_httpx(lines=[
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
        'data: {"type":"content_block_stop","index":0}',
    ])
    tx_path = tmp_path / "tx.log"
    async with Transcript(tx_path) as tx:
        provider = AnthropicProvider(api_key="sk-test", model="m")
        async for _ in provider.stream(
            system="SYS",
            messages=[{"role": "user", "content": "hi"}],
            transcript=tx,
            round_index=3,
        ):
            pass

    content = tx_path.read_text("utf-8")
    assert "round 3 request" in content
    assert '"model": "m"' in content
    assert "text_delta" in content


@pytest.mark.asyncio
async def test_no_api_key_raises_before_request(install_fake_httpx):
    install_fake_httpx(lines=[])
    provider = AnthropicProvider(api_key="", model="m")
    with pytest.raises(RuntimeError, match="API key not configured"):
        async for _ in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
            pass


@pytest.mark.asyncio
async def test_usage_chunk_yields_before_held_tool_use(install_fake_httpx):
    # Usage arrives in message_start (input + cache) and message_delta
    # (final output), both after/around the tool_use block on the wire.
    # The provider must reorder: one usage chunk first, then the held
    # tool_use -- the coordinator stops consuming at the first tool_use.
    install_fake_httpx(lines=[
        'data: {"type":"message_start","message":{"usage":{"input_tokens":120,"output_tokens":1,"cache_read_input_tokens":30,"cache_creation_input_tokens":10}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tu_1","name":"analyze"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{}"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","usage":{"output_tokens":57}}',
        'data: {"type":"message_stop"}',
    ])
    provider = AnthropicProvider(api_key="sk-test", model="m")
    chunks = []
    async for c in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
        chunks.append(c)

    assert [c.kind for c in chunks] == ["usage", "tool_use"]
    usage = chunks[0].usage
    assert usage.input_tokens == 120
    # message_delta's cumulative count overwrites message_start's initial 1.
    assert usage.output_tokens == 57
    assert usage.cache_read_input_tokens == 30
    assert usage.cache_creation_input_tokens == 10
    assert chunks[1].tool_use_id == "tu_1"


@pytest.mark.asyncio
async def test_usage_chunk_after_text_on_clean_round(install_fake_httpx):
    install_fake_httpx(lines=[
        'data: {"type":"message_start","message":{"usage":{"input_tokens":50,"output_tokens":2}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Done."}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","usage":{"output_tokens":9}}',
        'data: {"type":"message_stop"}',
    ])
    provider = AnthropicProvider(api_key="sk-test", model="m")
    chunks = []
    async for c in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
        chunks.append(c)

    assert [c.kind for c in chunks] == ["text", "usage"]
    assert chunks[1].usage.input_tokens == 50
    assert chunks[1].usage.output_tokens == 9
    # Fields the wire never sent stay at the dataclass default.
    assert chunks[1].usage.cache_read_input_tokens == 0


@pytest.mark.asyncio
async def test_usage_and_tool_use_flushed_without_message_stop(install_fake_httpx):
    # A stream that closes without message_stop (connection drop after the
    # last data line) must still surface the held tool_use and usage via
    # the defensive post-loop flush -- same order contract.
    install_fake_httpx(lines=[
        'data: {"type":"message_start","message":{"usage":{"input_tokens":80,"output_tokens":1}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"tu_9","name":"analyze"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{}"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_delta","usage":{"output_tokens":13}}',
    ])
    provider = AnthropicProvider(api_key="sk-test", model="m")
    chunks = []
    async for c in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
        chunks.append(c)

    assert [c.kind for c in chunks] == ["usage", "tool_use"]
    assert chunks[0].usage.input_tokens == 80
    assert chunks[0].usage.output_tokens == 13
    assert chunks[1].tool_use_id == "tu_9"


@pytest.mark.asyncio
async def test_mid_stream_error_event_raises(install_fake_httpx):
    install_fake_httpx(lines=[
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"start"}}',
        'data: {"type":"error","error":{"type":"overloaded_error","message":"server busy"}}',
    ])
    provider = AnthropicProvider(api_key="sk-test", model="m")
    with pytest.raises(RuntimeError, match="server busy"):
        async for _ in provider.stream(system="", messages=[{"role": "user", "content": "x"}]):
            pass
