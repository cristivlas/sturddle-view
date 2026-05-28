"""System prompt + mode addendum tests.

Covers:

- assembler properties: addendum routing, tools block rendering, determinism,
  error on unknown mode.
- coordinator wiring: the assembled prompt reaches `provider.stream()` as
  `system=...`, and the mode argument routes through.
- initial user message: side-to-move derivation, optional fields, move pairing.
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
    # The bulleted-list block (line starts with bare "Tools:") is absent;
    # the rules bullet "- Tools: ..." stays.
    assert "\nTools:\n" not in out


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
    assert provider.last_call["system"] == assemble_system_prompt("coach")


@pytest.mark.asyncio
async def test_coordinator_routes_mode_to_assembly():
    bus = EventBus()
    await bus.subscribe()
    provider = ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="ok")]])
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g", mode="commentator")

    assert provider.last_call is not None
    assert provider.last_call["system"] == assemble_system_prompt("commentator")


# ---------- Initial user message --------------------------------------


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


def test_user_message_includes_result_when_provided():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN, san_history=[], result="0-1",
    )
    assert "Game result: 0-1\n" in got


def test_user_message_omits_result_when_none():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN, san_history=[], result=None,
    )
    assert "Game result:" not in got


def test_user_message_includes_move_played_when_provided():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN, san_history=[], move_played="Bxg1",
    )
    assert "Move played here: Bxg1\n" in got


def test_user_message_omits_move_played_when_none():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN, san_history=[], move_played=None,
    )
    assert "Move played here:" not in got


def test_user_message_pairs_handle_odd_length():
    # White-to-move-next history (5 plies) ends on White's 3rd move with
    # no Black reply -- "1. e4 e5 2. Nf3 Nc6 3. Bb5".
    got = build_initial_user_message(
        fen="ignored-for-this-test",
        san_history=["e4", "e5", "Nf3", "Nc6", "Bb5"],
    )
    assert "1. e4 e5 2. Nf3 Nc6 3. Bb5" in got


# Annotations: imported PGN comments are surfaced to the commentator on a
# dedicated line so Game moves: stays byte-stable (prompt cache key).

_SAN5 = ["e4", "e5", "Nf3", "Nc6", "Bb5"]


def test_user_message_omits_annotations_when_none():
    baseline = build_initial_user_message(
        fen=_STARTPOS_FEN, san_history=_SAN5,
    )
    with_nones = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=_SAN5,
        annotations=None,
        root_annotation=None,
    )
    # Byte-stable: passing the new kwargs as None must not change output.
    # Prompt caching keys on these exact bytes.
    assert with_nones == baseline
    assert "Annotations:" not in baseline
    assert "Pre-game note:" not in baseline


def test_user_message_omits_annotations_when_all_none_entries():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=_SAN5,
        annotations=[None] * len(_SAN5),
    )
    assert "Annotations:" not in got


def test_user_message_renders_white_annotation_with_dot():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=_SAN5,
        annotations=["a sharp choice", None, None, None, None],
    )
    assert "Annotations: 1.e4 {a sharp choice}\n" in got


def test_user_message_renders_black_annotation_with_triple_dot():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=_SAN5,
        annotations=[None, "Petrov is also playable", None, None, None],
    )
    assert "Annotations: 1...e5 {Petrov is also playable}\n" in got


def test_user_message_renders_multiple_annotations_in_order():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=_SAN5,
        annotations=["sharp", None, None, None, "the Ruy"],
    )
    # Move 5 = (4 // 2) + 1 = 3; index 4 is white -> "3.Bb5".
    assert "Annotations: 1.e4 {sharp} 3.Bb5 {the Ruy}\n" in got


def test_user_message_renders_root_annotation_separately():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=_SAN5,
        root_annotation="Game from a 1972 candidates tournament.",
    )
    assert "Pre-game note: Game from a 1972 candidates tournament.\n" in got
    assert "Annotations:" not in got


def test_user_message_renders_root_and_per_ply_together():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=_SAN5,
        root_annotation="Famous miniature.",
        annotations=[None, "Petrov", None, None, None],
    )
    assert "Pre-game note: Famous miniature.\n" in got
    assert "Annotations: 1...e5 {Petrov}\n" in got
    # Pre-game note precedes Annotations.
    assert got.index("Pre-game note:") < got.index("Annotations:")


def test_user_message_game_moves_position_stable_when_annotations_added():
    # Game moves: position relative to surrounding lines must not shift
    # when annotations are present. Anything that changes the byte
    # offset of Game moves: breaks the prompt cache key.
    baseline = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=_SAN5,
        result="1-0",
        move_played="Bb5",
    )
    annotated = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=_SAN5,
        result="1-0",
        move_played="Bb5",
        root_annotation="Note.",
        annotations=["c", None, None, None, None],
    )
    # baseline is a strict prefix of annotated up to and including the
    # last line they share (Game result), so every cacheable field above
    # the new lines lands at byte-identical offsets.
    head = baseline.rstrip("\n")
    assert annotated.startswith(head)


def test_user_message_silently_drops_annotations_past_san_history():
    # Caller-side capping may have trimmed san_history but left a longer
    # annotations list; the renderer should not raise.
    got = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=["e4"],
        annotations=["one", "stray", "extras"],
    )
    assert "Annotations: 1.e4 {one}\n" in got
    assert "stray" not in got
    assert "extras" not in got


def test_user_message_empty_san_history_with_annotations_skips_line():
    got = build_initial_user_message(
        fen=_STARTPOS_FEN,
        san_history=[],
        annotations=["floating note"],
    )
    assert "Annotations:" not in got


def test_commentator_addendum_contains_critical_reading_directive():
    # The model must be told not to parrot imported author notes.
    assert "parrot" in COMMENTATOR_ADDENDUM
    assert "critically" in COMMENTATOR_ADDENDUM


def test_coach_addendum_does_not_contain_annotation_directive():
    # Annotations only flow in view/commentator mode; the coach addendum
    # should stay silent on the topic so coach prompts are not bloated
    # with unreachable instructions.
    assert "parrot" not in COACH_ADDENDUM


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
