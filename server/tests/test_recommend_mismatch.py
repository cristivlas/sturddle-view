"""Closing prose that names a move other than the recorded recommendation.

The on-board arrow is the accepted `recommend_move`; the prose is whatever
the final round wrote. Nothing tied the two together, so a turn could
record Qg3, draw the Qg3 arrow, and explain Ne3 -- both legal, so the
position check (illegal-move only) passed it clean.

The loop now re-prompts once naming the recorded move, and strikes the
offending spans if the model re-asserts. Prose that names the recorded move
alongside a rejected alternative ("Qg3 is stronger than Ne3") is legitimate
comparison and stays clean.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EVT_AI_POSITION_NOTE, EventBus
from sturddle_view.llm import (
    ProviderChunk,
    ScriptedProvider,
    ToolRegistry,
    ToolSpec,
)
from sturddle_view.llm.position_check import iter_stm_moves
from sturddle_view.play.ai_analysis import (
    AIAnalysisCoordinator,
    _RECOMMEND_MISMATCH_NUDGE,
)


# The semantic judge is a separate layer with its own suite; its extra
# stream() call would perturb the scripted-provider round budget here.
@pytest.fixture(autouse=True)
def _no_semantic_check(monkeypatch):
    monkeypatch.setattr(
        "sturddle_view.play.ai_analysis.SEMANTIC_CHECK_ENABLED", False,
    )


# The reported bug's actual position: White to move at 27, where the turn
# recorded Qg3 (the arrow) and the closing prose explained Ne3. Both legal.
_FEN = "1r2nr1k/2q1bp1P/p1bp2p1/4p3/1nN1P3/1PN2Q2/1BPR4/1K3BR1 w - - 1 27"


def _board() -> chess.Board:
    return chess.Board(_FEN)


def _uci(san: str) -> str:
    return _board().parse_san(san).uci()


def test_stm_moves_yields_legal_side_to_move_tokens():
    board = _board()
    found = {bare: move.uci() for _surface, bare, move in iter_stm_moves(
        "Qg3 keeps the initiative; Ne3 is the alternative.", board,
    )}
    assert found == {"Qg3": _uci("Qg3"), "Ne3": _uci("Ne3")}


def test_stm_moves_carries_surface_for_striking():
    surfaces = [s for s, _bare, _move in iter_stm_moves("Play Ne3 now.", _board())]
    assert surfaces == ["Ne3"]


def test_stm_moves_skips_black_marked_and_square_labels():
    # "...Nf6" is Black's by SAN convention, and "Rg1" names the rook already
    # on g1 (a label, not a move) -- neither is a White move to compare.
    board = _board()
    bares = [bare for _s, bare, _m in iter_stm_moves(
        "After ...Nf6 the Rg1 holds the file.", board,
    )]
    assert bares == []


def test_stm_moves_skips_other_ply_citations():
    # A numbered move at another ply is history/hypothetical, not this turn's.
    board = _board()
    bares = [bare for _s, bare, _m in iter_stm_moves("Back at 12.Ne3 it was murky.", board)]
    assert bares == []


def _coord(provider, registry=None):
    bus = EventBus()
    coord = AIAnalysisCoordinator(
        bus, provider,
        registry=registry or ToolRegistry(),
        board_provider=_board,
    )
    return coord, bus


def test_mismatch_flags_prose_naming_only_another_move():
    # The reported bug: recorded Qg3, prose explains Ne3.
    coord, _bus = _coord(provider=None)
    chunks = [ProviderChunk(
        kind="text",
        text="Ne3 repositions the knight to a central square while freeing f3.",
    )]
    result = coord._recommend_mismatch(chunks, _uci("Qg3"), "Qg3")
    assert result is not None
    surfaces, named = result
    assert named == "Ne3"
    assert surfaces == ["Ne3"]


def test_mismatch_clean_when_prose_names_the_recorded_move():
    coord, _bus = _coord(provider=None)
    chunks = [ProviderChunk(kind="text", text="Qg3 keeps the initiative.")]
    assert coord._recommend_mismatch(chunks, _uci("Qg3"), "Qg3") is None


def test_mismatch_allows_comparison_with_a_rejected_alternative():
    # Naming the recommendation alongside an alternative is real analysis,
    # not a mismatch -- the carve-out that keeps the check from over-firing.
    coord, _bus = _coord(provider=None)
    chunks = [ProviderChunk(
        kind="text", text="Qg3 is stronger than Ne3, which drops the e-pawn.",
    )]
    assert coord._recommend_mismatch(chunks, _uci("Qg3"), "Qg3") is None


def test_mismatch_clean_on_prose_naming_no_move_at_all():
    coord, _bus = _coord(provider=None)
    chunks = [ProviderChunk(kind="text", text="The initiative is worth the pawn.")]
    assert coord._recommend_mismatch(chunks, _uci("Qg3"), "Qg3") is None


async def _drain_until_done(queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


async def _stub_recommend(input_, *, cancel_token):
    move = _board().parse_san(input_["move"])
    return {"ok": True, "uci": move.uci(), "san": _board().san(move)}


def _recommend_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="recommend_move", description="rec", input_schema={"type": "object"},
        ),
        _stub_recommend,
    )
    return reg


def _last_user_texts(provider) -> list[str]:
    msgs = provider.last_call["messages"] if provider.last_call else []
    return [
        m["content"] for m in msgs
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    ]


@pytest.mark.asyncio
async def test_loop_reprompts_once_then_accepts_the_correction():
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="tool_use", tool_use_id="r0",
                       tool_name="recommend_move", tool_input={"move": "Qg3"})],
        [ProviderChunk(kind="text", text="Ne3 centralizes the knight.")],
        [ProviderChunk(kind="text", text="Qg3 keeps the initiative.")],
    ])
    coord, bus = _coord(provider, _recommend_registry())
    queue = await bus.subscribe()

    await coord.run(game_id="g", mode="coach")
    events = await _drain_until_done(queue)

    # The corrective forced the third round and named both moves.
    assert provider.stream_calls == 3
    injected = _last_user_texts(provider)
    assert any(
        t == _RECOMMEND_MISMATCH_NUDGE.format(san="Qg3", named="Ne3")
        for t in injected
    )
    # Corrected prose ships clean -- nothing struck.
    assert not [e for e in events if e.kind == EVT_AI_POSITION_NOTE]


@pytest.mark.asyncio
async def test_loop_strikes_reasserted_wrong_move_instead_of_looping():
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="tool_use", tool_use_id="r0",
                       tool_name="recommend_move", tool_input={"move": "Qg3"})],
        [ProviderChunk(kind="text", text="Ne3 centralizes the knight.")],
        [ProviderChunk(kind="text", text="Ne3 is still the move.")],
    ])
    coord, bus = _coord(provider, _recommend_registry())
    queue = await bus.subscribe()

    await coord.run(game_id="g", mode="coach")
    events = await _drain_until_done(queue)

    # One corrective only: the re-assertion is struck and shipped, so the
    # fourth round is never pulled.
    assert provider.stream_calls == 3
    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    assert notes[0].payload["surfaces"] == ["Ne3"]
