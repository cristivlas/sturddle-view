"""Ollama provider wire-level tests (mocked HTTP).

Covers behavior the live-daemon test cannot pin deterministically:
- request body is captured to the transcript before sending
- every raw SSE line is captured to the transcript, malformed included
- malformed JSON raises a clean RuntimeError (no silent skip)
- malformed tool_call.arguments flow back as a recoverable error chunk

Mocks httpx.AsyncClient at the module level (Ollama imports `httpx` by
name and calls `httpx.AsyncClient(...)`), which is the smallest surface
to stub without pulling in a new dep like respx.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from sturddle_view.llm import Transcript
from sturddle_view.llm import ollama as ollama_mod
from sturddle_view.llm import openai_compat as openai_compat_mod
from sturddle_view.llm.ollama import (
    OllamaProvider,
    openai_tool_call_to_provider_chunk,
)


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

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> None:
        return

    def stream(self, method, url, *, json=None, headers=None):
        self.last_body = json
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
    """Returns a setter: call install(lines) to plumb a fake stream."""
    holder: dict[str, _FakeClient | None] = {"client": None}

    def install(lines: list[str], status: int = 200, body: bytes = b"") -> None:
        resp = _FakeResponse(status_code=status, lines=lines, body=body)
        client = _FakeClient(resp)
        holder["client"] = client

        class _ShimHttpx:
            AsyncClient = lambda *a, **kw: client  # noqa: E731

        # The streaming SSE call lives in openai_compat now; control-plane
        # calls (list_models etc.) still use ollama's httpx. Patch both so
        # whichever path the test exercises is intercepted.
        monkeypatch.setattr(ollama_mod, "httpx", _ShimHttpx)
        monkeypatch.setattr(openai_compat_mod, "httpx", _ShimHttpx)

    install.holder = holder  # type: ignore[attr-defined]
    return install


# ---------- Tests ----------------------------------------------------


@pytest.mark.asyncio
async def test_request_body_captured_to_transcript(install_fake_httpx, tmp_path):
    install_fake_httpx(lines=[
        'data: {"choices":[{"delta":{"content":"hi"}}]}',
        "data: [DONE]",
    ])
    tx_path = tmp_path / "t.log"
    async with Transcript(tx_path) as tx:
        provider = OllamaProvider(base_url="http://fake", model="m")
        chunks = []
        async for c in provider.stream(
            system="SYS",
            messages=[{"role": "user", "content": "hello"}],
            transcript=tx,
            round_index=2,
        ):
            chunks.append(c)

    content = tx_path.read_text("utf-8")
    assert "round 2 request" in content
    assert '"model": "m"' in content
    assert '"role": "system"' in content
    assert '"content": "SYS"' in content
    assert '"role": "user"' in content
    assert '"content": "hello"' in content
    assert [c.text for c in chunks if c.kind == "text"] == ["hi"]


@pytest.mark.asyncio
async def test_compat_body_opts_into_usage_reporting(install_fake_httpx):
    install_fake_httpx(lines=["data: [DONE]"])
    provider = OllamaProvider(base_url="http://fake", model="m")
    async for _ in provider.stream(
        system="", messages=[{"role": "user", "content": "x"}],
    ):
        pass
    client = install_fake_httpx.holder["client"]
    assert client.last_body["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_native_usage_chunk_from_eval_counts(install_fake_httpx):
    # thinking_enabled routes to /api/chat (NDJSON). The done:true line
    # always carries prompt_eval_count / eval_count; they map onto a
    # usage chunk ordered before the held tool_use, matching the other
    # providers' contract. No cache fields on this surface.
    install_fake_httpx(lines=[
        '{"message":{"content":"checking"}}',
        '{"message":{"tool_calls":[{"function":{"name":"analyze","arguments":{}}}]}}',
        '{"done":true,"message":{},"prompt_eval_count":321,"eval_count":45}',
    ])
    provider = OllamaProvider(
        base_url="http://fake", model="m", thinking_enabled=True,
    )
    chunks = []
    async for c in provider.stream(
        system="", messages=[{"role": "user", "content": "x"}],
    ):
        chunks.append(c)

    kinds = [c.kind for c in chunks]
    assert kinds == ["text", "usage", "tool_use"]
    usage = chunks[1].usage
    assert usage.input_tokens == 321
    assert usage.output_tokens == 45
    assert usage.cache_read_input_tokens == 0
    assert usage.cache_creation_input_tokens == 0


@pytest.mark.asyncio
async def test_every_wire_line_captured(install_fake_httpx, tmp_path):
    lines = [
        'data: {"choices":[{"delta":{"content":"A"}}]}',
        "",                            # blank line in real SSE; skipped
        ": keepalive",                 # SSE comment; not "data:" so skipped
        'data: {"choices":[{"delta":{"content":"B"}}]}',
        "data: [DONE]",
    ]
    install_fake_httpx(lines=lines)

    tx_path = tmp_path / "t.log"
    async with Transcript(tx_path) as tx:
        provider = OllamaProvider(base_url="http://fake", model="m")
        async for _ in provider.stream(
            system="", messages=[], transcript=tx, round_index=0,
        ):
            pass

    content = tx_path.read_text("utf-8")
    # Every non-empty line should appear -- including the ":keepalive"
    # which the parser skips. Blank lines are dropped at the iterator.
    for line in lines:
        if not line:
            continue
        assert line in content, f"line not captured in transcript: {line!r}"


@pytest.mark.asyncio
async def test_malformed_sse_payload_raises_not_silent(install_fake_httpx, tmp_path):
    install_fake_httpx(lines=[
        'data: {"choices":[{"delta":{"content":"good"}}]}',
        "data: {this-is-not-json",
    ])
    tx_path = tmp_path / "t.log"
    async with Transcript(tx_path) as tx:
        provider = OllamaProvider(base_url="http://fake", model="m")
        with pytest.raises(RuntimeError, match="malformed SSE payload"):
            async for _ in provider.stream(
                system="", messages=[], transcript=tx, round_index=0,
            ):
                pass

    # The malformed line must still be in the transcript -- that's the
    # whole point of capturing before parsing.
    content = tx_path.read_text("utf-8")
    assert "{this-is-not-json" in content


@pytest.mark.asyncio
async def test_http_error_captured_then_raises(install_fake_httpx, tmp_path):
    install_fake_httpx(lines=[], status=500, body=b"server exploded")
    tx_path = tmp_path / "t.log"
    async with Transcript(tx_path) as tx:
        provider = OllamaProvider(base_url="http://fake", model="m")
        with pytest.raises(RuntimeError, match="ollama API error 500"):
            async for _ in provider.stream(
                system="", messages=[], transcript=tx, round_index=0,
            ):
                pass

    content = tx_path.read_text("utf-8")
    assert "HTTP 500" in content
    assert "server exploded" in content


# ---------- Tool-call argument JSON ----------------------------------


def test_malformed_tool_arguments_flow_as_error_chunk():
    # Bad JSON must NOT abort the turn. The chunk still flows as a
    # tool_use carrying the parse failure on `tool_input_error`, so the
    # coordinator hands the model a structured error to self-correct from.
    tc = {
        "id": "tu_x",
        "type": "function",
        "function": {"name": "analyze", "arguments": '{"fen": "rnb}'},
    }
    chunk = openai_tool_call_to_provider_chunk(tc)
    assert chunk.kind == "tool_use"
    assert chunk.tool_name == "analyze"
    assert chunk.tool_input == {}
    # Detail carries both the stdlib reason and the raw byte trail.
    assert chunk.tool_input_error
    assert '{"fen": "rnb}' in chunk.tool_input_error


def test_concatenated_tool_arguments_flow_as_error_chunk():
    # Real capture: model glued two top-level JSON objects onto one
    # tool_call.arguments. json.loads chokes on "Extra data"; we feed that
    # back instead of merging at the parser, so the model re-emits one call.
    raw = '{"family": "Sicilian"}{"depth": 15, "moves": ["cxd4"]}'
    tc = {
        "id": "tu_x",
        "type": "function",
        "function": {"name": "top_moves", "arguments": raw},
    }
    chunk = openai_tool_call_to_provider_chunk(tc)
    assert chunk.kind == "tool_use"
    assert chunk.tool_name == "top_moves"
    assert chunk.tool_input == {}
    assert chunk.tool_input_error
    assert "Extra data" in chunk.tool_input_error
    assert raw in chunk.tool_input_error


def test_valid_tool_arguments_still_parse():
    tc = {
        "id": "tu_x",
        "type": "function",
        "function": {"name": "analyze", "arguments": '{"fen": "rnb"}'},
    }
    chunk = openai_tool_call_to_provider_chunk(tc)
    assert chunk.tool_input == {"fen": "rnb"}


def test_empty_tool_arguments_yield_empty_dict():
    # Zero-argument tools may stream no arguments string at all; that is
    # not the same as malformed JSON and must not raise.
    tc = {
        "id": "tu_x",
        "type": "function",
        "function": {"name": "ping", "arguments": ""},
    }
    chunk = openai_tool_call_to_provider_chunk(tc)
    assert chunk.tool_input == {}


@pytest.mark.asyncio
async def test_reasoning_field_yields_thinking_chunks(install_fake_httpx):
    """Some Ollama models (nemotron-cascade) stream chain-of-thought
    into delta.reasoning instead of delta.content. The provider must
    surface those as kind=thinking so they at least appear in the
    transcript -- otherwise the model can run silently for minutes
    and the user sees nothing."""
    install_fake_httpx(lines=[
        'data: {"choices":[{"delta":{"reasoning":"thinking..."}}]}',
        'data: {"choices":[{"delta":{"content":"answer"}}]}',
        "data: [DONE]",
    ])

    provider = OllamaProvider(base_url="http://fake", model="m")
    chunks = []
    async for c in provider.stream(system="", messages=[]):
        chunks.append(c)

    kinds = [(c.kind, c.text) for c in chunks]
    assert ("thinking", "thinking...") in kinds
    assert ("text", "answer") in kinds
