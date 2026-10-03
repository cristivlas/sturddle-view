"""LLM judging of regex position-check flags (position_judge).

Every flag is false on the live board; the judge only tags the ones the prose
places in another (earlier or later) position. A tag never adds a flag. These
tests lock that one-directional contract and the parse robustness (a
malformed or hallucinated verdict must never drop a flag), plus the loop
wiring: the judge is told each item's board fact, a tagged flag is not struck
and its one-time rephrase request only rides along with a round that follows
anyway, and the env flag disables the layer.
"""
from __future__ import annotations

from typing import AsyncIterator

import chess
import pytest

from sturddle_view.events import EVT_AI_POSITION_NOTE, EventBus
from sturddle_view.llm import ProviderChunk, ToolRegistry
from sturddle_view.llm.base import LLMProvider, Message, ToolWireSpec
from sturddle_view.llm.position_judge import (
    _parse_other_position_labels,
    judge_other_position,
)
from sturddle_view.play.ai_analysis import _POSITION_CHECK_PREFIX, AIAnalysisCoordinator


# Black to move; e4 empty, so "Be4" is illegal-on-this-board -> a regex flag.
_FEN = "r2qr1k1/5ppp/p4n2/1pbP1bB1/8/2Nn1B2/PP1Q1PPP/1N1R1RK1 b - - 3 17"
_BE4_ITEM = ("Be4", "Be4 isn't legal for the side to move")

# Wild: a weak Ollama judge cleared "bishop on d7" here (2026-09-29); Black has
# no bishops and d7 is empty. The judge was never told what the square holds.
_WILD_D7_FEN = "1r1r2k1/2p2pp1/1p1q3p/p3p3/1nP5/1P1PR1PP/P2Q1PN1/1R4K1 b - - 0 23"
_WILD_D7_PROSE = (
    "Your light-squared bishop on d7 controls long diagonals against "
    "White's king-side castled position."
)

_KEEP_ALL = 'VERDICT {"other_position": []}'


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


# -- _parse_other_position_labels -------------------------------------------


def test_parse_extracts_labels_after_tag():
    out = _parse_other_position_labels('VERDICT {"other_position": ["Be4"]}', ["Be4", "Nd3"])
    assert out == {"Be4"}


def test_parse_tolerates_prose_around_json():
    text = 'Sure.\nVERDICT {"other_position": ["Nd3"]}\nDone.'
    assert _parse_other_position_labels(text, ["Be4", "Nd3"]) == {"Nd3"}


def test_parse_drops_hallucinated_label_not_in_candidates():
    # A label the regex never flagged can't be tagged.
    assert _parse_other_position_labels('VERDICT {"other_position": ["Qz9"]}', ["Be4"]) == set()


def test_parse_empty_list():
    assert _parse_other_position_labels(_KEEP_ALL, ["Be4"]) == set()


@pytest.mark.parametrize("text", ["", "no json here", "VERDICT not-json", "VERDICT {bad}"])
def test_parse_failure_tags_nothing(text):
    # The safe direction: a malformed verdict keeps every flag.
    assert _parse_other_position_labels(text, ["Be4"]) == set()


def test_parse_non_list_tags_nothing():
    assert _parse_other_position_labels('VERDICT {"other_position": "Be4"}', ["Be4"]) == set()


# -- judge_other_position ---------------------------------------------------


@pytest.mark.asyncio
async def test_judge_returns_subset_of_labels():
    judge = _CannedJudge('VERDICT {"other_position": ["Be4"]}')
    out = await judge_other_position(judge, chess.Board(_FEN), "...Be4 was strong.", [_BE4_ITEM])
    assert out == {"Be4"}
    assert judge.calls == 1


@pytest.mark.asyncio
async def test_judge_forces_thinking_off():
    judge = _CannedJudge(_KEEP_ALL)
    await judge_other_position(judge, chess.Board(_FEN), "prose", [_BE4_ITEM])
    assert judge.last_thinking is False


@pytest.mark.asyncio
async def test_no_items_skips_provider_call():
    judge = _CannedJudge('VERDICT {"other_position": ["Be4"]}')
    out = await judge_other_position(judge, chess.Board(_FEN), "clean prose", [])
    assert out == set()
    assert judge.calls == 0


@pytest.mark.asyncio
async def test_provider_error_keeps_all_flags():
    judge = _CannedJudge(raise_on_call=True)
    items = [_BE4_ITEM, ("Nd3", "Nd3 isn't legal for the side to move")]
    out = await judge_other_position(judge, chess.Board(_FEN), "prose", items)
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


class _Scripted(LLMProvider):
    """Replays `replies` in stream() call order ("" once exhausted) and records
    each call's (system, messages). The loop calls the narrator, runs the regex
    check, then (this layer) the judge -- so one provider serves both."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls = 0
        self.requests: list[tuple[str, list[Message]]] = []

    async def stream(self, system, messages, tools=None, *, transcript=None,
                     round_index=0, thinking=None,
                     force_tool_call=False) -> AsyncIterator[ProviderChunk]:
        # Copy: the loop keeps appending to the same list after this call.
        self.requests.append((system, list(messages)))
        reply = self._replies[self.calls] if self.calls < len(self._replies) else ""
        self.calls += 1
        yield ProviderChunk(kind="text", text=reply)


def _position_check_asks(messages: list[Message]) -> list[str]:
    return [
        m["content"] for m in messages
        if m["role"] == "user" and isinstance(m["content"], str)
        and m["content"].startswith(_POSITION_CHECK_PREFIX)
    ]


@pytest.mark.asyncio
async def test_judge_is_told_what_the_flagged_square_holds():
    # The wild miss: the judge got only the FEN. It must be handed the board
    # fact behind each flag so it needn't decode the FEN itself.
    provider = _Scripted(_WILD_D7_PROSE, _KEEP_ALL)
    coord, bus = _coord(provider, chess.Board(_WILD_D7_FEN))
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    await _drain_until_done(queue)

    _system, judge_messages = provider.requests[1]
    assert "d7 is empty" in judge_messages[-1]["content"]


_TAG_BH4 = 'VERDICT {"other_position": ["Bh4"]}'


@pytest.mark.asyncio
async def test_other_position_flag_alone_is_not_struck_and_forces_no_round():
    board = chess.Board(_FEN)
    # "Bh4" is illegal here -> regex flags it. The judge places it in another
    # position: no strike (no note), and no round is forced just to ask for a
    # rephrase -- the rephrased prose would repeat the unstruck original.
    provider = _Scripted("Earlier, Bh4 had been tempting.", _TAG_BH4)
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert not any(e.kind == EVT_AI_POSITION_NOTE for e in events)
    assert provider.calls == 2  # narrator + judge


@pytest.mark.asyncio
async def test_rephrase_rides_along_once_with_a_corrective():
    board = chess.Board(_FEN)
    # Qh2 / Ra2 are kept flags, so each draws a corrective round anyway; the
    # tagged Bh4 rides along with the first, and is not asked about again.
    provider = _Scripted(
        "Earlier, Bh4 had been tempting. Black plays Qh2 now.", _TAG_BH4,
        "Earlier, Bh4 had been tempting. Black plays Ra2 now.", _TAG_BH4,
    )
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    await _drain_until_done(queue)

    first_ask = _position_check_asks(provider.requests[2][1])[-1]
    second_ask = _position_check_asks(provider.requests[4][1])[-1]
    assert "Qh2" in first_ask and "Bh4" in first_ask
    assert "Ra2" in second_ask and "Bh4" not in second_ask


@pytest.mark.asyncio
async def test_judge_keeping_flag_emits_note():
    board = chess.Board(_FEN)
    # Judge tags nothing -> the flag stands -> a position note fires and the
    # loop injects a corrective (a second narrator round follows).
    provider = _Scripted("Black plays Bh4 now.", _KEEP_ALL)
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
    # even though the judge reply would have tagged it had the layer run.
    provider = _Scripted("Black plays Bh4 now.", 'VERDICT {"other_position": ["Bh4"]}')
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert any(e.kind == EVT_AI_POSITION_NOTE for e in events)
