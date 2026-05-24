"""Scripted provider for agent-loop tests.

Phase 1 promotes `CannedProvider` (text-only) to `ScriptedProvider`
(text + tool_use). Each `stream()` call consumes one pre-scripted round
from the queue. Tests pass a list of rounds and assert the runner's
behavior across them -- text emission, tool dispatch, tool_result
re-injection into the next round's messages.

Why a separate class rather than extending CannedProvider:
- Phase 0 tests pin the no-tool behavior; mixing tool_use into
  CannedProvider blurs the contract.
- ScriptedProvider is stateful (consumes one round per call). Canned is
  stateless (replays the same chunks every call). Keeping them separate
  makes the difference loud.
"""
from __future__ import annotations

from collections import deque
from typing import AsyncIterator, Iterable

from .base import LLMProvider, Message, ProviderChunk, ToolWireSpec


class ScriptedProvider(LLMProvider):
    def __init__(self, rounds: Iterable[Iterable[ProviderChunk]]) -> None:
        # Materialize per-round chunks so the script is deterministic
        # even when the caller hands us a generator.
        self._rounds: deque[tuple[ProviderChunk, ...]] = deque(
            tuple(r) for r in rounds
        )
        self._stream_calls = 0
        self._last_call: dict | None = None

    @property
    def stream_calls(self) -> int:
        """Number of times stream() has been invoked. Lets tests assert
        the runner made exactly N rounds without inspecting the wire."""
        return self._stream_calls

    @property
    def last_call(self) -> dict | None:
        """Frozen snapshot of the most recent stream() invocation:
        {system, messages, tools}. Tests assert the runner is building
        the next round's input correctly (e.g. appending tool_result)."""
        return self._last_call

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        self._stream_calls += 1
        # Deep-copy messages so a later mutation by the runner can't
        # rewrite history visible to the test assertion.
        self._last_call = {
            "system": system,
            "messages": [dict(m) for m in messages],
            "tools": list(tools) if tools is not None else None,
        }
        if not self._rounds:
            raise RuntimeError(
                "ScriptedProvider exhausted: stream() called more times "
                "than rounds were scripted"
            )
        chunks = self._rounds.popleft()
        for chunk in chunks:
            yield chunk
