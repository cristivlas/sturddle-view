"""Opening links in the AIAnalysisCoordinator loop.

A narrator round whose prose passes the position check emits
ai_opening_links for the openings the turn put in front of the model -- the
game's own and every related_openings row: each item is the exact prose
surface plus the opening's line (UCI from the start position) the client
plays on double-click. An opening the model names on its own is not linked,
and a flagged round's prose is struck instead, so it carries no links.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EVT_AI_OPENING_LINKS, EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry
from sturddle_view.openings import Opening, OpeningBook
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator
from sturddle_view.play.tools_openings import (
    RELATED_OPENINGS_TOOL_NAME,
    RELATED_OPENINGS_TOOL_SPEC,
    make_related_openings_tool,
)


# The flagged-round test locks regex-loop behavior; the semantic judge's
# extra stream() call would perturb the scripted round budget.
@pytest.fixture(autouse=True)
def _no_semantic_check(monkeypatch):
    monkeypatch.setattr(
        "sturddle_view.play.ai_analysis.SEMANTIC_CHECK_ENABLED", False,
    )


# Black to move; h6 is empty (a false-claim target).
_FEN = "r2qr1k1/5ppp/p4n2/1pbP1bB1/8/2Nn1B2/PP1Q1PPP/1N1R1RK1 b - - 3 17"
_ITALIAN_GAME = "Italian Game"
_ITALIAN_GAME_UCI = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]
_CARO_KANN = "Caro-Kann Defense"
_CARO_KANN_UCI = ["e2e4", "c7c6"]
_CARO_KANN_EXCHANGE = "Caro-Kann Defense: Exchange Variation"


def _opening(name: str) -> Opening:
    return next(o for o in OpeningBook.load().all() if o.name == name)


def _text(text: str) -> list[ProviderChunk]:
    return [ProviderChunk(kind="text", text=text)]


async def _link_events(coord, bus, **run_kwargs) -> list:
    queue = await bus.subscribe()
    await coord.run(game_id="g", **run_kwargs)
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return [e for e in events if e.kind == EVT_AI_OPENING_LINKS]


async def _run(rounds, *, opening=None, board=None) -> list:
    """Link events of a tool-free turn in `opening`."""
    bus = EventBus()
    coord = AIAnalysisCoordinator(
        bus, ScriptedProvider(rounds=rounds), registry=ToolRegistry(),
        board_provider=(lambda: board) if board is not None else None,
        book_provider=OpeningBook.load,
    )
    return await _link_events(coord, bus, opening=opening)


@pytest.mark.asyncio
async def test_clean_round_links_the_games_opening():
    links = await _run(
        [_text("The Italian Game is solid.")], opening=_opening(_ITALIAN_GAME),
    )
    assert len(links) == 1
    assert links[0].payload["round"] == 0
    assert links[0].payload["items"] == [
        {"surface": _ITALIAN_GAME, "uci": _ITALIAN_GAME_UCI},
    ]


@pytest.mark.asyncio
async def test_opening_the_turn_never_showed_is_not_linked():
    links = await _run(
        [_text("The Italian Game is solid.")], opening=_opening(_CARO_KANN),
    )
    assert links == []


@pytest.mark.asyncio
async def test_short_form_inside_another_openings_name_is_not_linked():
    # The book tells "French Defense Exchange Variation" is another opening,
    # so its tail is not taken for the game's own Exchange Variation.
    opening = _opening(_CARO_KANN_EXCHANGE)
    links = await _run(
        [_text(
            "Unlike the French Defense Exchange Variation, "
            "this Exchange Variation is calm."
        )],
        opening=opening,
    )
    # The other name rides along unlinked so the client leaves it whole.
    assert [e.payload["items"] for e in links] == [[
        {"surface": "French Defense Exchange Variation", "uci": None},
        {"surface": "Exchange Variation", "uci": list(opening.moves)},
    ]]


@pytest.mark.asyncio
async def test_no_opening_emits_nothing():
    assert await _run([_text("The Italian Game is solid.")]) == []


@pytest.mark.asyncio
async def test_flagged_round_is_not_linked():
    links = await _run(
        [
            _text("The bishop on h6 dominates, as in the Italian Game."),
            _text("Corrected: the Italian Game plan still applies."),
        ],
        opening=_opening(_ITALIAN_GAME),
        board=chess.Board(_FEN),
    )
    assert [e.payload["round"] for e in links] == [1]


@pytest.mark.asyncio
async def test_related_openings_rows_are_linked():
    # The tool tells the coordinator what it returned; the next round's prose
    # naming one of those rows links to that row's line.
    bus = EventBus()
    registry = ToolRegistry()
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_1",
            tool_name=RELATED_OPENINGS_TOOL_NAME,
            tool_input={"family": _CARO_KANN},
        )],
        _text("The Caro-Kann Defense is the sturdier choice."),
    ])
    coord = AIAnalysisCoordinator(bus, provider, registry=registry)
    registry.register(
        RELATED_OPENINGS_TOOL_SPEC,
        make_related_openings_tool(
            book_provider=OpeningBook.load,
            board_provider=lambda: None,
            on_shown=coord.note_openings,
        ),
    )
    links = await _link_events(coord, bus)
    assert [e.payload["items"] for e in links] == [
        [{"surface": _CARO_KANN, "uci": _CARO_KANN_UCI}],
    ]


@pytest.mark.asyncio
async def test_turn_openings_do_not_leak_into_the_next_turn():
    bus = EventBus()
    provider = ScriptedProvider(rounds=[
        _text("The Italian Game is solid."),
        _text("The Italian Game is solid."),
    ])
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())
    first = await _link_events(coord, bus, opening=_opening(_ITALIAN_GAME))
    second = await _link_events(coord, bus)
    assert len(first) == 1
    assert second == []
