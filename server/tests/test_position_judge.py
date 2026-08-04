"""LLM clearing of regex position-check false positives (position_judge).

The judge can only *clear* a flag, never add one. These tests lock that
one-directional contract and the parse robustness (a malformed or
hallucinated verdict must never drop a flag), plus the loop wiring: a cleared
flag reads as clean prose (no note, no corrective), and the env flag disables
the layer.
"""
from __future__ import annotations

from typing import AsyncIterator

import chess
import pytest

from sturddle_view.events import EVT_AI_POSITION_NOTE, EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry
from sturddle_view.llm.base import LLMProvider, Message, ToolWireSpec
from sturddle_view.llm.position_judge import (
    _parse_clear_labels,
    clear_false_positives,
)
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator


# Black to move; e4 empty, so "Be4" is illegal-on-this-board -> a regex flag.
_FEN = "r2qr1k1/5ppp/p4n2/1pbP1bB1/8/2Nn1B2/PP1Q1PPP/1N1R1RK1 b - - 3 17"


class _CannedJudge(LLMProvider):
    """Provider that replays one fixed text reply on every stream() call --
    stands in for the judge's model. `raise_on_call` simulates a provider
    error to exercise the fail-safe (keep all flags)."""

    def __init__(self, reply: str = "", raise_on_call: bool = False) -> None:
        self._reply = reply
        self._raise = raise_on_call
        self.calls = 0
        self.last_thinking: bool | None = "unset"

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None = None,
        *,
        transcript=None,
        round_index: int = 0,
        thinking: bool | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        self.calls += 1
        self.last_thinking = thinking
        if self._raise:
            raise RuntimeError("boom")
        yield ProviderChunk(kind="text", text=self._reply)


# -- _parse_clear_labels ----------------------------------------------------


def test_parse_extracts_labels_after_tag():
    out = _parse_clear_labels('VERDICT {"clear": ["Be4"]}', ["Be4", "Nd3"])
    assert out == {"Be4"}


def test_parse_tolerates_prose_around_json():
    text = 'Sure.\nVERDICT {"clear": ["Nd3"]}\nDone.'
    assert _parse_clear_labels(text, ["Be4", "Nd3"]) == {"Nd3"}


def test_parse_drops_hallucinated_label_not_in_candidates():
    # A label the regex never flagged can't be cleared.
    assert _parse_clear_labels('VERDICT {"clear": ["Qz9"]}', ["Be4"]) == set()


def test_parse_empty_clear_list():
    assert _parse_clear_labels('VERDICT {"clear": []}', ["Be4"]) == set()


@pytest.mark.parametrize("text", ["", "no json here", "VERDICT not-json", "VERDICT {bad}"])
def test_parse_failure_clears_nothing(text):
    # The safe direction: a malformed verdict keeps every flag.
    assert _parse_clear_labels(text, ["Be4"]) == set()


def test_parse_non_list_clear_clears_nothing():
    assert _parse_clear_labels('VERDICT {"clear": "Be4"}', ["Be4"]) == set()


# -- clear_false_positives --------------------------------------------------


@pytest.mark.asyncio
async def test_clear_returns_subset_of_labels():
    judge = _CannedJudge('VERDICT {"clear": ["Be4"]}')
    out = await clear_false_positives(judge, chess.Board(_FEN), "...Be4 was strong.", ["Be4"])
    assert out == {"Be4"}
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_clear_forces_thinking_off():
    judge = _CannedJudge('VERDICT {"clear": []}')
    await clear_false_positives(judge, chess.Board(_FEN), "prose", ["Be4"])
    assert judge.last_thinking is False


@pytest.mark.asyncio
async def test_no_labels_skips_provider_call():
    judge = _CannedJudge('VERDICT {"clear": ["Be4"]}')
    out = await clear_false_positives(judge, chess.Board(_FEN), "clean prose", [])
    assert out == set()
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_provider_error_keeps_all_flags():
    judge = _CannedJudge(raise_on_call=True)
    out = await clear_false_positives(judge, chess.Board(_FEN), "prose", ["Be4", "Nd3"])
    assert out == set()


# -- loop wiring ------------------------------------------------------------


def _coord(provider, board):
    bus = EventBus()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(), board_provider=lambda: board,
    )
    return coord, bus


async def _drain_until_done(queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


class _NarratorThenJudge(LLMProvider):
    """Narrator on the first stream() call, judge on the second. The loop
    calls the narrator, runs the regex check, then (this layer) the judge --
    so a single provider serves both in call order."""

    def __init__(self, narrator_text: str, judge_reply: str) -> None:
        self._replies = [narrator_text, judge_reply]
        self.calls = 0

    async def stream(self, system, messages, tools=None, *, transcript=None,
                     round_index=0, thinking=None,
                     force_tool_call=False) -> AsyncIterator[ProviderChunk]:
        reply = self._replies[self.calls] if self.calls < len(self._replies) else ""
        self.calls += 1
        yield ProviderChunk(kind="text", text=reply)


@pytest.mark.asyncio
async def test_judge_clearing_flag_suppresses_note():
    board = chess.Board(_FEN)
    # "Bh4" is illegal here -> regex flags it. The judge clears it, so the
    # round reads clean: no position note, and the narrator exits naturally.
    provider = _NarratorThenJudge(
        narrator_text="Earlier, Bh4 had been tempting.",
        judge_reply='VERDICT {"clear": ["Bh4"]}',
    )
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert not any(e.kind == EVT_AI_POSITION_NOTE for e in events)
    assert provider.calls == 2  # narrator + judge, no corrective re-round


@pytest.mark.asyncio
async def test_judge_keeping_flag_emits_note():
    board = chess.Board(_FEN)
    # Judge clears nothing -> the flag stands -> a position note fires and the
    # loop injects a corrective (a second narrator round follows).
    provider = _NarratorThenJudge(
        narrator_text="Black plays Bh4 now.",
        judge_reply='VERDICT {"clear": []}',
    )
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert any(e.kind == EVT_AI_POSITION_NOTE for e in events)


@pytest.mark.asyncio
async def test_env_flag_off_skips_judge(monkeypatch):
    monkeypatch.setattr(
        "sturddle_view.play.ai_analysis.SEMANTIC_CHECK_ENABLED", False,
    )
    board = chess.Board(_FEN)
    # With the judge off, the flag stands and a note fires (regex-only path),
    # even though the judge_reply would have cleared it had the layer run.
    provider = _NarratorThenJudge(
        narrator_text="Black plays Bh4 now.",
        judge_reply='VERDICT {"clear": ["Bh4"]}',
    )
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert any(e.kind == EVT_AI_POSITION_NOTE for e in events)
