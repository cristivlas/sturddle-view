"""Opening links in the AIAnalysisCoordinator loop.

A narrator round whose prose passes the position check emits
ai_opening_links: each opening name's exact prose surface plus the book
line (UCI from the start position) the client plays on double-click. A
flagged round's prose is struck instead, so it carries no links.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EVT_AI_OPENING_LINKS, EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry
from sturddle_view.openings import OpeningBook
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator


# The flagged-round test locks regex-loop behavior; the semantic judge's
# extra stream() call would perturb the scripted round budget.
@pytest.fixture(autouse=True)
def _no_semantic_check(monkeypatch):
    monkeypatch.setattr(
        "sturddle_view.play.ai_analysis.SEMANTIC_CHECK_ENABLED", False,
    )


# Black to move; h6 is empty (a false-claim target).
_FEN = "r2qr1k1/5ppp/p4n2/1pbP1bB1/8/2Nn1B2/PP1Q1PPP/1N1R1RK1 b - - 3 17"
_ITALIAN_GAME_UCI = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]


async def _link_events(rounds, *, book=True, board=None) -> list:
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text=text)] for text in rounds
    ])
    bus = EventBus()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(),
        board_provider=(lambda: board) if board is not None else None,
        book_provider=OpeningBook.load if book else None,
    )
    queue = await bus.subscribe()
    await coord.run(game_id="g")
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return [e for e in events if e.kind == EVT_AI_OPENING_LINKS]


@pytest.mark.asyncio
async def test_clean_round_links_opening_names():
    links = await _link_events(["The Italian Game is solid."])
    assert len(links) == 1
    assert links[0].payload["round"] == 0
    assert links[0].payload["items"] == [
        {"surface": "Italian Game", "uci": _ITALIAN_GAME_UCI},
    ]


@pytest.mark.asyncio
async def test_prose_without_opening_names_emits_nothing():
    assert await _link_events(["Black is slightly better here."]) == []


@pytest.mark.asyncio
async def test_no_book_emits_nothing():
    assert await _link_events(["The Italian Game is solid."], book=False) == []


@pytest.mark.asyncio
async def test_flagged_round_is_not_linked():
    links = await _link_events(
        [
            "The bishop on h6 dominates, as in the Italian Game.",
            "Corrected: the Italian Game plan still applies.",
        ],
        board=chess.Board(_FEN),
    )
    assert [e.payload["round"] for e in links] == [1]
