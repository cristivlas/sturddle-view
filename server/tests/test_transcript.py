"""Conversation transcript: writer, opt-in plumbing, integration.

Three layers:

1. `Transcript` writer unit tests: file format, append behavior,
   serialization of all event kinds, close idempotency.
2. `open_transcript` env-gated behavior: SV_AI_TRANSCRIPT off yields a
   NullTranscript (no file created); on yields a real writer.
3. Coordinator integration: with transcripts enabled and a
   ScriptedProvider, a real run() produces a file containing every
   expected event marker in order.
"""
from __future__ import annotations

import asyncio

import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import (
    NullTranscript,
    ProviderChunk,
    ScriptedProvider,
    Transcript,
    open_transcript,
    transcript_enabled,
)
from sturddle_view.llm.transcript import (
    TRANSCRIPT_ENV_VAR,
    TURN_SEPARATOR,
)
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator


# ---------- Writer unit tests -----------------------------------------


@pytest.mark.asyncio
async def test_writer_creates_file_and_writes_blocks(tmp_path):
    path = tmp_path / "t.log"
    async with Transcript(path) as tx:
        await tx.turn_start({"mode": "coach", "game_id": "g1"})
        await tx.system_prompt("SYS")
        await tx.user_message("USR")
        await tx.request(0, {"model": "m", "messages": []})
        await tx.wire_line(0, "data: {}")
        await tx.chunk(0, ProviderChunk(kind="text", text="hi"))
        await tx.tool_result(0, "tu_1", {"ok": True})
        await tx.turn_end({"done": True})

    content = path.read_text("utf-8")
    # Every event label must appear, in order.
    for marker in (
        "[turn_start]", "[system]", "[user]",
        "round 0 request", "round 0 wire", "round 0 chunk",
        "round 0 tool_result", "[turn_end]",
    ):
        assert marker in content, f"missing marker {marker!r} in transcript"
    # Turn separator is emitted on turn_start so multiple turns are
    # visually distinguishable in a rolling file.
    assert TURN_SEPARATOR in content


@pytest.mark.asyncio
async def test_writer_appends_across_turns(tmp_path):
    path = tmp_path / "rolling.log"
    async with Transcript(path) as tx:
        await tx.turn_start({"n": 1})
        await tx.turn_end({"done": True})
    async with Transcript(path) as tx:
        await tx.turn_start({"n": 2})
        await tx.turn_end({"done": True})

    content = path.read_text("utf-8")
    assert content.count(TURN_SEPARATOR) == 2
    assert '"n": 1' in content
    assert '"n": 2' in content


@pytest.mark.asyncio
async def test_writer_close_is_idempotent(tmp_path):
    path = tmp_path / "t.log"
    tx = Transcript(path)
    await tx.close()
    await tx.close()  # must not raise


@pytest.mark.asyncio
async def test_writer_handles_non_json_serializable(tmp_path):
    # A real tool result could contain types json can't natively serialize;
    # the writer must fall back rather than crash mid-turn.
    path = tmp_path / "t.log"
    async with Transcript(path) as tx:
        await tx.tool_result(0, "tu", {"obj": object()})
    content = path.read_text("utf-8")
    assert "tool_result" in content
    assert "tu" in content


@pytest.mark.asyncio
async def test_writer_serializes_concurrent_writes(tmp_path):
    # The internal lock guarantees no torn lines when two coroutines
    # write concurrently. Without the lock, OS file buffering plus async
    # interleaving can split lines.
    path = tmp_path / "concur.log"
    async with Transcript(path) as tx:
        await asyncio.gather(*[
            tx.wire_line(0, f"line-{i}") for i in range(20)
        ])

    content = path.read_text("utf-8")
    for i in range(20):
        assert f"line-{i}" in content


# ---------- open_transcript: env gating --------------------------------


@pytest.mark.asyncio
async def test_open_transcript_off_yields_null_writer(monkeypatch, tmp_path):
    monkeypatch.delenv(TRANSCRIPT_ENV_VAR, raising=False)
    target = tmp_path / "should-not-exist.log"
    async with open_transcript(path=target) as tx:
        assert isinstance(tx, NullTranscript)
        await tx.system_prompt("SYS")  # no-op
    assert not target.exists()


@pytest.mark.asyncio
async def test_open_transcript_on_writes_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv(TRANSCRIPT_ENV_VAR, "1")
    target = tmp_path / "subdir" / "t.log"
    async with open_transcript(path=target) as tx:
        assert not isinstance(tx, NullTranscript)
        await tx.system_prompt("hello")
    assert target.exists()
    assert "hello" in target.read_text("utf-8")


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("no", False),
])
def test_transcript_enabled_reads_env(monkeypatch, val, expected):
    monkeypatch.setenv(TRANSCRIPT_ENV_VAR, val)
    assert transcript_enabled() is expected


def test_transcript_enabled_absent_env_is_false(monkeypatch):
    monkeypatch.delenv(TRANSCRIPT_ENV_VAR, raising=False)
    assert transcript_enabled() is False


# ---------- Coordinator integration -----------------------------------


@pytest.mark.asyncio
async def test_coordinator_writes_full_turn_to_transcript(monkeypatch, tmp_path):
    monkeypatch.setenv(TRANSCRIPT_ENV_VAR, "1")
    target = tmp_path / "ai-transcript.log"
    # Patch the default-path resolver so the coordinator writes to tmp.
    from sturddle_view.llm import transcript as tx_mod
    monkeypatch.setattr(tx_mod, "default_transcript_path", lambda: target)

    bus = EventBus()
    await bus.subscribe()
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="Hello.")],
    ])
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g1", user_message="FEN: ...")
    assert target.exists()

    content = target.read_text("utf-8")
    # Coordinator-level lifecycle.
    assert "[turn_start]" in content
    assert '"game_id": "g1"' in content
    assert "ScriptedProvider" in content
    assert "[system]" in content
    assert "You are a chess analysis assistant." in content
    assert "[user]" in content
    assert "FEN: ..." in content
    assert "round 0 chunk" in content
    assert '"text": "Hello."' in content
    assert "[turn_end]" in content
    assert '"done": true' in content


@pytest.mark.asyncio
async def test_transcript_off_means_no_file_after_run(monkeypatch, tmp_path):
    monkeypatch.delenv(TRANSCRIPT_ENV_VAR, raising=False)
    target = tmp_path / "ai-transcript.log"
    from sturddle_view.llm import transcript as tx_mod
    monkeypatch.setattr(tx_mod, "default_transcript_path", lambda: target)

    bus = EventBus()
    await bus.subscribe()
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="ok")],
    ])
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g1")
    assert not target.exists(), "transcript file must not be created when env var is off"
