"""Ollama provider wire-level tests (mocked HTTP).

Covers behavior the live-daemon test cannot pin deterministically:
- request body is captured to the transcript before sending
- every raw SSE line is captured to the transcript, malformed included
- malformed JSON raises a clean RuntimeError (no silent skip)
- malformed tool_call.arguments raises MalformedToolArgumentsError

Mocks httpx.AsyncClient at the module level (Ollama imports `httpx` by
name and calls `httpx.AsyncClient(...)`), which is the smallest surface
to stub without pulling in a new dep like respx.
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest

from sturddle_view.llm import Transcript
from sturddle_view.llm import ollama as ollama_mod
from sturddle_view.llm.ollama import (
    MalformedToolArgumentsError,
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

        monkeypatch.setattr(ollama_mod, "httpx", _ShimHttpx)

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


def test_malformed_tool_arguments_raises():
    tc = {
        "id": "tu_x",
        "type": "function",
        "function": {"name": "analyze", "arguments": '{"fen": "rnb}'},
    }
    with pytest.raises(MalformedToolArgumentsError) as ei:
        openai_tool_call_to_provider_chunk(tc)
    err = ei.value
    assert err.tool_name == "analyze"
    assert err.raw_arguments == '{"fen": "rnb}'
    # parse_error carries json.JSONDecodeError's message verbatim; we
    # don't pin the exact wording (it's a stdlib implementation detail),
    # only that some explanation made it through.
    assert err.parse_error


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
