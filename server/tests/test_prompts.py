"""System prompt + mode addendum tests.

Two layers:

1. **Byte-stability tripwires.** The full assembled prompt is pinned to
   a frozen literal. Accidental drift (env reads, format-string changes,
   stray whitespace) breaks the test. Intentional prompt edits also
   break the test -- the author must paste the new value in, which is
   the moment they notice prompt caching has been invalidated.

2. **Coordinator wiring.** The coordinator passes the assembled prompt
   to `provider.stream()` as `system=...`. `ScriptedProvider` captures
   the call; we assert the system field is the coach prompt (default)
   or whatever was passed via `mode=`.
"""
from __future__ import annotations

import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import (
    ProviderChunk,
    ScriptedProvider,
    assemble_system_prompt,
    build_initial_user_message,
)
from sturddle_view.llm.prompts import (
    COACH_ADDENDUM,
    COMMENTATOR_ADDENDUM,
    SYSTEM_PROMPT,
)
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator


_STARTPOS_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


_EXPECTED_COACH = (
    "You are a chess analysis assistant. You collaborate with a chess engine "
    "that produces numeric evaluations, principal variations, and search "
    "depth. The engine is the source of truth for any numeric claim; your "
    "job is to turn its output into clear prose for a human reader.\n"
    "\n"
    "Ground rules:\n"
    "- Cite engine numbers explicitly when you make a claim about an "
    "evaluation. Tool results carry both `score_cp` (centipawns, integer; "
    "100 cp = 1 pawn) and `score_text` (presentation string like '+0.02' "
    "or '+M3'). Use `score_text` for prose; never present `score_cp` as "
    "if it were pawns.\n"
    "- If you have not seen an engine evaluation for the position you are "
    "discussing, call the analyze tool before claiming anything about it. "
    "Do not guess.\n"
    "- You have a bounded tool-call budget per turn. Prefer one well-aimed "
    "analyze call over several speculative ones.\n"
    "- Be concise. A few sentences of grounded prose beats a paragraph of "
    "hedging.\n"
    "- Never invent moves, lines, or evaluations. If the engine output does "
    "not support a claim, say so.\n"
    "\n"
    "You are coaching a human player during a live game against an engine. "
    "Address the player in the second person.\n"
    "\n"
    "Focus on the position in front of the player right now: what their "
    "last move accomplished or missed, what threats and ideas are on the "
    "board, and what to look for on the next move. Do not reveal the "
    "opponent engine's planned continuation -- coach the player on what "
    "they can see and decide for themselves.\n"
)


_EXPECTED_COMMENTATOR = (
    "You are a chess analysis assistant. You collaborate with a chess engine "
    "that produces numeric evaluations, principal variations, and search "
    "depth. The engine is the source of truth for any numeric claim; your "
    "job is to turn its output into clear prose for a human reader.\n"
    "\n"
    "Ground rules:\n"
    "- Cite engine numbers explicitly when you make a claim about an "
    "evaluation. Tool results carry both `score_cp` (centipawns, integer; "
    "100 cp = 1 pawn) and `score_text` (presentation string like '+0.02' "
    "or '+M3'). Use `score_text` for prose; never present `score_cp` as "
    "if it were pawns.\n"
    "- If you have not seen an engine evaluation for the position you are "
    "discussing, call the analyze tool before claiming anything about it. "
    "Do not guess.\n"
    "- You have a bounded tool-call budget per turn. Prefer one well-aimed "
    "analyze call over several speculative ones.\n"
    "- Be concise. A few sentences of grounded prose beats a paragraph of "
    "hedging.\n"
    "- Never invent moves, lines, or evaluations. If the engine output does "
    "not support a claim, say so.\n"
    "\n"
    "You are annotating a chess game for a reader who is reviewing it after "
    "the fact. Write in the third person, in the style of a chess magazine "
    "annotator.\n"
    "\n"
    "Identify critical moments -- blunders, missed wins, key strategic "
    "decisions -- and explain them with reference to the engine "
    "evaluations. The reader can see the whole game, so feel free to "
    "reference what happens later when it illuminates an earlier moment.\n"
)


def test_coach_prompt_byte_stable():
    assert assemble_system_prompt("coach") == _EXPECTED_COACH


def test_commentator_prompt_byte_stable():
    assert assemble_system_prompt("commentator") == _EXPECTED_COMMENTATOR


def test_coach_prompt_contains_coach_addendum_only():
    out = assemble_system_prompt("coach")
    assert SYSTEM_PROMPT in out
    assert COACH_ADDENDUM in out
    assert COMMENTATOR_ADDENDUM not in out


def test_commentator_prompt_contains_commentator_addendum_only():
    out = assemble_system_prompt("commentator")
    assert SYSTEM_PROMPT in out
    assert COMMENTATOR_ADDENDUM in out
    assert COACH_ADDENDUM not in out


def test_unknown_mode_raises_value_error():
    with pytest.raises(ValueError, match="unknown prompt mode"):
        assemble_system_prompt("analyst")  # type: ignore[arg-type]


def test_assembly_is_deterministic_across_calls():
    # Same input -> same bytes, every call. If anyone slips in an env
    # read or a timestamp later, repeated calls will diverge.
    a = assemble_system_prompt("coach")
    b = assemble_system_prompt("coach")
    assert a == b
    assert a is not b or True  # identity not required; equality is


@pytest.mark.asyncio
async def test_coordinator_passes_coach_prompt_by_default():
    bus = EventBus()
    await bus.subscribe()
    provider = ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="ok")]])
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g")

    assert provider.last_call is not None
    assert provider.last_call["system"] == _EXPECTED_COACH


@pytest.mark.asyncio
async def test_coordinator_routes_mode_to_assembly():
    bus = EventBus()
    await bus.subscribe()
    provider = ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="ok")]])
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g", mode="commentator")

    assert provider.last_call is not None
    assert provider.last_call["system"] == _EXPECTED_COMMENTATOR


# ---------- Initial user message --------------------------------------


_EXPECTED_USER_STARTPOS_NO_MOVES = (
    f"Current position (FEN): {_STARTPOS_FEN}\n"
    "Game moves: (none yet -- the game has not started)\n"
)

_EXPECTED_USER_AFTER_E4_E5_NF3 = (
    "Current position (FEN): "
    "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2\n"
    "Game moves: 1. e4 e5 2. Nf3\n"
)


def test_user_message_startpos_no_moves_byte_stable():
    got = build_initial_user_message(fen=_STARTPOS_FEN, san_history=[])
    assert got == _EXPECTED_USER_STARTPOS_NO_MOVES


def test_user_message_with_moves_byte_stable():
    got = build_initial_user_message(
        fen="rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
        san_history=["e4", "e5", "Nf3"],
    )
    assert got == _EXPECTED_USER_AFTER_E4_E5_NF3


def test_user_message_pairs_handle_odd_length():
    # White-to-move-next history (5 plies) ends on White's 3rd move with
    # no Black reply -- "1. e4 e5 2. Nf3 Nc6 3. Bb5".
    got = build_initial_user_message(
        fen="ignored-for-this-test",
        san_history=["e4", "e5", "Nf3", "Nc6", "Bb5"],
    )
    assert "1. e4 e5 2. Nf3 Nc6 3. Bb5" in got


@pytest.mark.asyncio
async def test_coordinator_passes_user_message_to_provider():
    bus = EventBus()
    await bus.subscribe()
    provider = ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="ok")]])
    coord = AIAnalysisCoordinator(bus, provider)

    user_msg = build_initial_user_message(fen=_STARTPOS_FEN, san_history=[])
    await coord.run(game_id="g", user_message=user_msg)

    assert provider.last_call is not None
    assert provider.last_call["messages"] == [
        {"role": "user", "content": user_msg}
    ]


@pytest.mark.asyncio
async def test_coordinator_empty_when_no_user_message():
    # Back-compat: existing tests that call run() without user_message
    # must continue to see an empty opening user message.
    bus = EventBus()
    await bus.subscribe()
    provider = ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="ok")]])
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g")

    assert provider.last_call is not None
    assert provider.last_call["messages"] == [{"role": "user", "content": ""}]
