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
    ToolSpec,
    assemble_system_prompt,
    build_initial_user_message,
)
from sturddle_view.llm.prompts import (
    COACH_ADDENDUM,
    COMMENTATOR_ADDENDUM,
    SYSTEM_PROMPT_PREFACE,
    SYSTEM_PROMPT_RULES,
)
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator


_STARTPOS_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


_EXPECTED_PREAMBLE = (
    "You are a chess analyst. Form your own judgment about the position "
    "using your chess understanding -- opening theory, pawn structures, "
    "piece coordination, plans, typical patterns and motifs. An engine "
    "is available as a tool for concrete tactical verification and lines "
    "you cannot calculate; it is a sanity check on your thinking, not a "
    "substitute for it.\n"
    "\n"
    "Ground rules:\n"
    "- Voice: never write in the first person. No self-reference, no "
    "narration of your own thinking, recognition, or process. Address "
    "the reader directly in the voice the mode addendum specifies. "
    "Open with chess content, not with a sentence about what you are "
    "doing.\n"
    "- Length: output exactly 3 to 5 sentences. After the 5th sentence, "
    "your turn ends -- do not begin a sixth.\n"
    "- Content: every sentence names a square, a piece on a square, a "
    "candidate move, a tactical motif, or a structural feature. The "
    "first sentence must name one of these, not set a scene or "
    "characterize the position generally. Sentences that only describe "
    "mood, balance, or vague intent are removed before output.\n"
    "- Eval discipline: the reader sees the engine's numeric evaluation "
    "in the UI. Do not state, quote, paraphrase, or characterize it in "
    "any form. Use the engine's numbers internally to choose what to "
    "discuss; never as content.\n"
    "- Notation: SAN only.\n"
    "- Side to move and point of view: engine scores are White-POV "
    "regardless of whose turn it is. The user message states the side "
    "to move; trust that field. Never re-derive it from the FEN.\n"
    "- Knowledge use: lead with chess understanding -- opening name, "
    "pawn structure, plan, pattern. Tools are reserved for concrete "
    "tactical lines and confirmation of variations you cannot "
    "calculate. Express knowledge as chess facts, not as observations "
    "about your own cognition.\n"
    "- Tool results carry `score_cp` (centipawns; 100 cp = 1 pawn) and "
    "`score_text` (presentation string). These exist so you can reason "
    "about magnitude internally; neither appears in your prose (see "
    "Eval discipline).\n"
    "- Tool budget: bounded calls per turn. Prefer one well-aimed call "
    "over several speculative ones.\n"
    "- Engine name: use the name given in the user message. Do not "
    "invent another.\n"
    "- Honesty: do not invent moves, lines, or evaluations. If the "
    "engine output does not support a claim, say so.\n"
    "- Format: plain text only. No Markdown, no LaTeX math, no code "
    "fences, no headings, no bullet lists.\n"
    "\n"
)


_EXPECTED_COACH = _EXPECTED_PREAMBLE + (
    "Address the reader in the second person throughout. The reader is "
    "the player to move in a live game; never refer to them as \"White\" "
    "or \"Black\" -- they are \"you\" and the opponent is \"your opponent\" "
    "or \"the engine\". Offer your own assessment of the position and "
    "what the reader should be thinking about for the next move. Do not "
    "reveal the opponent engine's planned continuation.\n"
)


_EXPECTED_COMMENTATOR = _EXPECTED_PREAMBLE + (
    "Post-game review; the reader sees the whole game. Write in the third "
    "person, in the style of a chess magazine annotator. Offer your own "
    "assessment of critical moments and the strategic ideas driving each "
    "side. May reference later moves when they illuminate the current one.\n"
)


def test_coach_prompt_byte_stable():
    assert assemble_system_prompt("coach") == _EXPECTED_COACH


def test_commentator_prompt_byte_stable():
    assert assemble_system_prompt("commentator") == _EXPECTED_COMMENTATOR


def test_coach_prompt_contains_coach_addendum_only():
    out = assemble_system_prompt("coach")
    assert SYSTEM_PROMPT_PREFACE in out
    assert SYSTEM_PROMPT_RULES.rstrip("\n") in out
    assert COACH_ADDENDUM in out
    assert COMMENTATOR_ADDENDUM not in out


def test_commentator_prompt_contains_commentator_addendum_only():
    out = assemble_system_prompt("commentator")
    assert SYSTEM_PROMPT_PREFACE in out
    assert SYSTEM_PROMPT_RULES.rstrip("\n") in out
    assert COMMENTATOR_ADDENDUM in out
    assert COACH_ADDENDUM not in out


def test_unknown_mode_raises_value_error():
    with pytest.raises(ValueError, match="unknown prompt mode"):
        assemble_system_prompt("analyst")  # type: ignore[arg-type]


def test_tools_block_rendered_from_registry():
    """The Tools: section is built by iterating registered tools, so
    description drift (prompt vs. wire) can't happen. Confirms the
    block uses each tool's `description` field verbatim."""
    tools = [
        ToolSpec(name="analyze", description="Run engine on FEN.", input_schema={}),
        ToolSpec(name="hypothetical", description="Imaginary thing.", input_schema={}),
    ]
    out = assemble_system_prompt("coach", tools=tools)
    assert "Tools:\n" in out
    assert "- `analyze`: Run engine on FEN.\n" in out
    assert "- `hypothetical`: Imaginary thing.\n" in out


def test_no_tools_block_when_registry_empty():
    out = assemble_system_prompt("coach", tools=[])
    assert "Tools:" not in out


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
    "Side to move: white\n"
    "Game moves: (none yet -- the game has not started)\n"
)

_EXPECTED_USER_AFTER_E4_E5_NF3 = (
    "Current position (FEN): "
    "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2\n"
    "Side to move: black\n"
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


def test_user_message_side_to_move_derived_from_fen():
    white = build_initial_user_message(fen=_STARTPOS_FEN, san_history=[])
    assert "Side to move: white\n" in white
    black = build_initial_user_message(
        fen="rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 2",
        san_history=["e4", "e5"],
    )
    assert "Side to move: black\n" in black


def test_user_message_malformed_fen_falls_back_to_white():
    out = build_initial_user_message(fen="not-a-real-fen", san_history=[])
    assert "Side to move: white\n" in out


def test_user_message_includes_engine_name_when_provided():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN, san_history=[], engine_name="MyEngine 1.0",
    )
    assert got.startswith("Engine: MyEngine 1.0\n")


def test_user_message_includes_opening_with_eco_prefix():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=["e4", "e5", "Nf3", "Nc6"],
        opening_eco="C44",
        opening_name="King's Pawn Game",
    )
    assert "Opening: [C44] King's Pawn Game\n" in got


def test_user_message_opening_name_without_eco():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN, san_history=["e4"], opening_name="King's Pawn",
    )
    assert "Opening: King's Pawn\n" in got
    assert "[" not in got.split("Opening:")[1].split("\n")[0]


def test_user_message_omits_optional_fields_when_absent():
    # Byte-stable back-compat: no engine, no opening -> original 3-line shape.
    got = build_initial_user_message(fen=_STARTPOS_FEN, san_history=[])
    assert got == _EXPECTED_USER_STARTPOS_NO_MOVES


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
