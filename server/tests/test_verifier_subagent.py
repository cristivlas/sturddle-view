"""Planner + verifier subagent path in AIAnalysisCoordinator.

The narrator delegates a move-verification question via the `delegate`
tool; the coordinator runs a verifier sub-run (own prompt, verifier
registry) and feeds its verdict back as the tool result. These tests
lock the contracts the live-test debugging surfaced:

- the verifier inherits the turn's position context (else it can't form
  a tool call and begs for the FEN),
- its verdict is the final round's prose only (no cross-round self-talk),
- it runs with thinking forced off while the narrator keeps the setting,
- its tool-call events forward to the UI stamped with the delegate's id
  (nesting) while its prose stays internal,
- the post-recommend nudge fires only when a move was accepted without a
  closing conclusion.

ScriptedProvider serves a single round queue; the narrator and verifier
sub-run pull from it in call order. Rounds are told apart by the system
prompt (verifier prompt differs) and the `thinking` flag recorded below.
"""
from __future__ import annotations

import asyncio

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import (
    Message,
    ProviderChunk,
    TOOL_SIGNATURE_KEY,
    ToolRegistry,
    ToolSpec,
)
from sturddle_view.llm.base import LLMProvider, ToolWireSpec
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.llm.prompts import VERIFIER_ADDENDUM
from sturddle_view.play.ai_analysis import (
    AIAnalysisCoordinator,
    DELEGATE_TOOL_SPEC,
    _EMPTY_TURN_PLACEHOLDER,
    _assistant_message,
    make_delegate_tool,
)


# A distinctive phrase from VERIFIER_ADDENDUM, present only in the
# verifier's assembled system prompt -- lets tests tell verifier rounds
# from narrator rounds by content, not just position.
_VERIFIER_PROMPT_MARKER = VERIFIER_ADDENDUM.split(".")[0]


_START_FEN = chess.STARTING_FEN
_TURN_CONTEXT = f"Current position (FEN): {_START_FEN}\nSide to move: white"


class _RecordingScriptedProvider(LLMProvider):
    """Like ScriptedProvider but records each stream() call's system
    prompt, messages snapshot, and `thinking` flag so tests can tell
    narrator rounds from verifier rounds and assert thinking-off."""

    def __init__(self, rounds):
        self._rounds = [tuple(r) for r in rounds]
        self._i = 0
        self.calls: list[dict] = []

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolWireSpec] | None = None,
        *,
        transcript=None,
        round_index: int = 0,
        thinking: bool | None = None,
    ):
        self.calls.append({
            "system": system,
            "messages": [dict(m) for m in messages],
            "thinking": thinking,
        })
        if self._i >= len(self._rounds):
            raise RuntimeError("provider exhausted: more stream() calls than rounds")
        chunks = self._rounds[self._i]
        self._i += 1
        for chunk in chunks:
            yield chunk


async def _noop_tool(_input, *, cancel_token):
    return {"ok": True}


def _verifier_registry() -> ToolRegistry:
    """A verifier registry with one stand-in engine tool the sub-run can
    call. Real engine tools aren't needed to exercise the loop wiring."""
    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="piece_at", description="piece_at", input_schema={"type": "object"}),
        _noop_tool,
    )
    return reg


def _coordinator(bus, provider, *, board=None):
    # delegate now parses its `move` against the live board, so default to
    # the start position when a test doesn't supply one.
    if board is None:
        board = chess.Board()
    reg = ToolRegistry()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg,
        board_provider=(lambda: board),
        verifier_registry=_verifier_registry(),
    )
    reg.register(
        DELEGATE_TOOL_SPEC,
        make_delegate_tool(coord.delegate_runner(), board_provider=(lambda: board)),
    )
    return coord


async def _drain_until_done(queue: asyncio.Queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


def _delegate_chunk(
    tool_use_id: str, question: str, move: str = "e4",
) -> ProviderChunk:
    return ProviderChunk(
        kind="tool_use",
        tool_use_id=tool_use_id,
        tool_name="delegate",
        tool_input={"move": move, "question": question},
    )


@pytest.mark.asyncio
async def test_delegate_reports_illegal_vs_invalid_with_fen():
    # A bad `move` must surface the real kind (illegal_move vs invalid_move)
    # and the FEN it validated against, not a flat "could not parse" -- so a
    # wrong-side-to-move pick (e.g. '...d5' on white's turn) is legible. The
    # parse fails before the verifier runs, so the runner is never called.
    async def never(_question):
        raise AssertionError("runner must not run when the move is rejected")

    board = chess.Board()  # white to move
    delegate = make_delegate_tool(never, board_provider=(lambda: board))

    illegal = await delegate(
        {"move": "...d5", "question": "best?"}, cancel_token=CancelToken(),
    )
    assert illegal["error"] == "illegal_move"
    assert illegal["fen"] == board.fen()

    invalid = await delegate(
        {"move": "zz9", "question": "best?"}, cancel_token=CancelToken(),
    )
    assert invalid["error"] == "invalid_move"
    assert invalid["fen"] == board.fen()


@pytest.mark.asyncio
async def test_verifier_inherits_turn_position_context():
    # Narrator delegates -> verifier sub-run -> verifier concludes.
    # Verifier must call a tool before concluding (addendum + nudge), so
    # its first round calls one, then concludes.
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("d1", "Is e4 sound?")],            # narrator round 0
        [ProviderChunk(                                     # verifier round 0: tool
            kind="tool_use", tool_use_id="v1",
            tool_name="piece_at", tool_input={"square": "e2"},
        )],
        [ProviderChunk(kind="text", text="e4 is sound.")],  # verifier round 1: verdict
        [ProviderChunk(kind="text", text="Done.")],         # narrator round 1
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider)

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # The verifier round is the one whose first user message carries the
    # delegated question; assert it ALSO carries the turn's FEN context.
    verifier_call = next(
        c for c in provider.calls
        if "Is e4 sound?" in c["messages"][0]["content"]
    )
    user0 = verifier_call["messages"][0]["content"]
    assert _START_FEN in user0, "verifier lost the position context"
    assert "Is e4 sound?" in user0


@pytest.mark.asyncio
async def test_verifier_round_cap_surfaces_on_done_payload():
    # A delegate whose verifier never concludes (only ever calls tools)
    # caps out; the narrator turn flags verifier_round_cap so the UI can
    # show the gear note pointing at the verifier-rounds setting.
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("d1", "Is e4 sound?")],            # narrator round 0
        [ProviderChunk(                                     # verifier: tool, never concludes
            kind="tool_use", tool_use_id="v1",
            tool_name="piece_at", tool_input={"square": "e2"},
        )],
        [ProviderChunk(                                     # verifier: still a tool -> cap=1 hit
            kind="tool_use", tool_use_id="v2",
            tool_name="piece_at", tool_input={"square": "e4"},
        )],
        [ProviderChunk(kind="text", text="Done.")],         # narrator round 1
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider)

    await coord.run(
        game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n",
        verifier_max_rounds=1,
    )
    events = await _drain_until_done(queue)

    done = events[-1]
    assert done.payload.get("verifier_round_cap") is True
    # The narrator itself finished cleanly -- this is advisory, not a failure.
    assert not done.payload.get("round_cap")


@pytest.mark.asyncio
async def test_verifier_thinking_forced_off_narrator_inherits():
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("d1", "check e4")],                # narrator
        [ProviderChunk(                                     # verifier: tool
            kind="tool_use", tool_use_id="v1",
            tool_name="piece_at", tool_input={"square": "e2"},
        )],
        [ProviderChunk(kind="text", text="sound.")],        # verifier: verdict
        [ProviderChunk(kind="text", text="done.")],         # narrator
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider)

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # Correlate the thinking flag with WHICH prompt ran, not just counts:
    # every verifier-prompt round must be thinking=False, every other
    # (narrator) round thinking=None. A swapped override fails here.
    for c in provider.calls:
        is_verifier = _VERIFIER_PROMPT_MARKER in c["system"]
        if is_verifier:
            assert c["thinking"] is False, "verifier round should force thinking off"
        else:
            assert c["thinking"] is None, "narrator round should inherit thinking"
    assert sum(_VERIFIER_PROMPT_MARKER in c["system"] for c in provider.calls) == 2


@pytest.mark.asyncio
async def test_verifier_verdict_is_final_round_prose_only():
    # Verifier emits self-talk, calls a tool, THEN concludes. The verdict
    # fed back must be the final round's prose, not the pre-tool chatter.
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("d1", "check e4")],                      # narrator
        [                                                         # verifier r0
            ProviderChunk(kind="text", text="Let me check first. "),
            ProviderChunk(
                kind="tool_use", tool_use_id="v1",
                tool_name="piece_at", tool_input={"square": "e2"},
            ),
        ],
        [ProviderChunk(kind="text", text="e4 is sound.")],       # verifier r1
        [ProviderChunk(kind="text", text="Conclusion.")],        # narrator
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider)

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # The narrator's final round sees the delegate tool_result; its content
    # is the verdict. Must be the final-round prose, not the self-talk.
    narrator_final = provider.calls[-1]
    tool_result = next(
        b
        for m in narrator_final["messages"] if m["role"] == "user"
        and isinstance(m["content"], list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    assert "e4 is sound." in tool_result["content"]
    assert "Let me check first." not in tool_result["content"]


@pytest.mark.asyncio
async def test_verifier_tool_events_carry_parent_id_and_prose_suppressed():
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("d1", "check e4")],
        [                                                     # verifier round
            ProviderChunk(kind="text", text="internal verdict text"),
            ProviderChunk(
                kind="tool_use", tool_use_id="v1",
                tool_name="piece_at", tool_input={"square": "e2"},
            ),
        ],
        [ProviderChunk(kind="text", text="e4 sound.")],       # verifier concludes
        [ProviderChunk(kind="text", text="done.")],           # narrator
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider)

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    # Verifier tool-call events forward, stamped with the delegate's id.
    verifier_tool_calls = [
        e for e in events
        if e.kind == "ai_tool_call" and e.payload.get("name") == "piece_at"
    ]
    assert verifier_tool_calls, "verifier tool call did not surface"
    assert all(
        e.payload.get("parent_tool_use_id") == "d1" for e in verifier_tool_calls
    )

    # The verifier's verdict reaches the narrator (as the delegate
    # tool_result) but is NOT streamed to the UI -- only the narrator's
    # own prose appears as ai_info deltas. Asserting both sides proves the
    # prose was routed internally, not merely absent.
    # Find the delegate's tool_result (the one carrying the verdict),
    # not the verifier's internal piece_at result.
    delegate_results = [
        b
        for c in provider.calls
        for m in c["messages"] if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
        and "verdict" in b["content"]
    ]
    assert delegate_results, "delegate verdict tool_result not found"
    assert "e4 sound." in delegate_results[0]["content"], "verdict didn't reach narrator"
    deltas = [e.payload["delta"] for e in events if "delta" in e.payload]
    assert "internal verdict text" not in deltas
    assert "e4 sound." not in deltas
    assert "done." in deltas, "narrator prose should stream"


@pytest.mark.asyncio
async def test_post_recommend_nudge_fires_when_no_conclusion():
    # Move accepted, then the model exits with no prose: the nudge runs
    # one more round to extract the conclusion.
    async def recommend(_input, *, cancel_token):
        return {"ok": True, "uci": "e2e4"}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(                                       # round 0: accept e4, no prose
            kind="tool_use", tool_use_id="r0",
            tool_name="recommend_move", tool_input={"move": "e4"},
        )],
        [],                                                   # round 1: still no prose -> nudge
        [ProviderChunk(kind="text", text="The plan is to control e5.")],  # round 2: conclusion
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: None),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # Round 0 accepts the move; round 1 exits with no conclusion -> the
    # nudge injects a prompt and runs round 2. The nudge's user message
    # (last user message before round 2's stream) asks for the conclusion.
    assert len(provider.calls) == 3
    nudge_round = provider.calls[2]
    last_user = next(
        m for m in reversed(nudge_round["messages"]) if m["role"] == "user"
    )
    assert "conclusion" in last_user["content"].lower()


@pytest.mark.asyncio
async def test_no_post_recommend_nudge_when_conclusion_same_round():
    # Conclusion prose + recommend_move in the SAME round: no extra round.
    async def recommend(_input, *, cancel_token):
        return {"ok": True, "uci": "e2e4"}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [                                                     # round 0: conclusion + move
            ProviderChunk(kind="text", text="e4 grabs the center."),
            ProviderChunk(
                kind="tool_use", tool_use_id="r0",
                tool_name="recommend_move", tool_input={"move": "e4"},
            ),
        ],
        [],                                                   # round 1: model stops, no prose
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: None),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # A round always follows the accepting call (the call ends round 0).
    # The same-round conclusion sets prose_after_recommend, so round 1's
    # empty exit does NOT trigger the nudge: exactly 2 rounds, no 3rd.
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_rejected_recommend_is_renudged_until_accepted():
    # First attempt rejected as not-the-strongest; the completeness nudge
    # re-fires (not one-shot) and the second, accepted attempt resolves it.
    calls = {"n": 0}

    async def recommend(_input, *, cancel_token):
        calls["n"] += 1
        board = chess.Board()
        uci = board.parse_san(_input["move"]).uci()
        # r0's move is rejected (a stronger move named); r1's is accepted.
        if calls["n"] == 1:
            return {"error": "recommendation_rejected", "reason": "A stronger move is available: Nf3."}
        return {"ok": True, "uci": uci}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(                                    # r0: attempt e4 -> rejected
            kind="tool_use", tool_use_id="r0",
            tool_name="recommend_move", tool_input={"move": "e4"},
        )],
        [ProviderChunk(                                    # r1 (nudged): submit the named move
            kind="tool_use", tool_use_id="r1",
            tool_name="recommend_move", tool_input={"move": "Nf3"},
        )],
        [ProviderChunk(kind="text", text="Developing toward the center.")],  # r2: conclusion
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: None),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    # r0 rejected and re-nudged; r1 accepted, so the turn ends cleanly.
    assert calls["n"] == 2
    done = events[-1]
    assert not done.payload.get("no_recommendation")
    assert not done.payload.get("round_cap")


@pytest.mark.asyncio
async def test_stalled_recommend_stops_nudging_and_flags_no_recommendation():
    # Attempt rejected, then the model stalls (prose, no new attempt). The
    # nudge does not loop forever: one re-nudge, then it gives up and the
    # turn ends flagged no_recommendation (distinct from round_cap).
    async def recommend(_input, *, cancel_token):
        return {"error": "recommendation_rejected", "reason": "Engine prefers Nf3."}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(                                    # r0: attempt -> rejected
            kind="tool_use", tool_use_id="r1",
            tool_name="recommend_move", tool_input={"move": "e4"},
        )],
        [ProviderChunk(kind="text", text="I will not commit.")],  # r1: clean exit -> nudge #1
        [ProviderChunk(kind="text", text="Still nothing.")],      # r2: clean exit, no new attempt
        [ProviderChunk(kind="text", text="Should never run.")],   # r3: guard vs over-looping
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: None),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    # r0 attempt (rejected) -> r1 clean exit draws nudge #1 -> r2 clean exit
    # with no new attempt trips the stall guard -> stop. Exactly 3 rounds;
    # the 4th scripted round is never pulled (no infinite re-nudge).
    assert len(provider.calls) == 3
    done = events[-1]
    assert done.payload.get("no_recommendation") is True
    assert not done.payload.get("round_cap")


async def _echo_verifier(move, depth, cancel_token):
    # Stand-in end-of-turn verifier: echoes the move so an ai_recommendation
    # event (the on-board arrow) fires for the accepted move.
    return {"uci": move.uci(), "san": move.uci()}


@pytest.mark.asyncio
async def test_commentator_accept_without_compare_is_held_once():
    # Commentator must not endorse the reviewed move with zero contrast: the
    # first accept with no prior top_moves is held with a compare_first error,
    # one-shot. After a top_moves call the resubmit goes through.
    async def recommend(_input, *, cancel_token):
        return {"ok": True, "uci": chess.Board().parse_san(_input["move"]).uci()}

    async def top_moves(_input, *, cancel_token):
        return {"candidates": [{"move_uci": "e2e4", "move_san": "e4", "score_cp": 20}]}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    reg.register(
        ToolSpec(name="top_moves", description="rank", input_schema={"type": "object"}),
        top_moves,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(kind="tool_use", tool_use_id="r0",     # accept w/o compare -> held
                       tool_name="recommend_move", tool_input={"move": "e4"})],
        [ProviderChunk(kind="tool_use", tool_use_id="t0",     # comply: compare
                       tool_name="top_moves", tool_input={"moves": ["e4", "d4"]})],
        [ProviderChunk(kind="tool_use", tool_use_id="r1",     # resubmit -> accepted
                       tool_name="recommend_move", tool_input={"move": "e4"})],
        [ProviderChunk(kind="text", text="e4 is best on review.")],  # conclusion
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_echo_verifier,
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    held = [
        e for e in events if e.kind == "ai_tool_call_failed"
        and e.payload.get("error") == "compare_first"
    ]
    assert len(held) == 1, "first uncompared accept should be held once"
    recs = [e for e in events if e.kind == "ai_recommendation"]
    assert recs and recs[-1].payload.get("uci") == "e2e4"


@pytest.mark.asyncio
async def test_commentator_accept_after_top_moves_not_held():
    # A top_moves call before the accept satisfies the compare-first check:
    # the recommend goes straight through, no compare_first hold.
    async def recommend(_input, *, cancel_token):
        return {"ok": True, "uci": chess.Board().parse_san(_input["move"]).uci()}

    async def top_moves(_input, *, cancel_token):
        return {"candidates": [{"move_uci": "e2e4", "move_san": "e4", "score_cp": 20}]}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    reg.register(
        ToolSpec(name="top_moves", description="rank", input_schema={"type": "object"}),
        top_moves,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(kind="tool_use", tool_use_id="t0",     # compare first
                       tool_name="top_moves", tool_input={"moves": ["e4", "d4"]})],
        [ProviderChunk(kind="tool_use", tool_use_id="r0",     # accept -> not held
                       tool_name="recommend_move", tool_input={"move": "e4"})],
        [ProviderChunk(kind="text", text="e4 holds up.")],    # conclusion
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_echo_verifier,
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    assert not [
        e for e in events if e.kind == "ai_tool_call_failed"
        and e.payload.get("error") == "compare_first"
    ], "a prior top_moves should satisfy compare-first"
    recs = [e for e in events if e.kind == "ai_recommendation"]
    assert recs and recs[-1].payload.get("uci") == "e2e4"


@pytest.mark.asyncio
async def test_coach_accept_without_compare_not_held():
    # The compare-first hold is commentator-only: coach (live play) accepts
    # the first recommend directly, no top_moves required.
    async def recommend(_input, *, cancel_token):
        return {"ok": True, "uci": chess.Board().parse_san(_input["move"]).uci()}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(kind="tool_use", tool_use_id="r0",
                       tool_name="recommend_move", tool_input={"move": "e4"})],
        [ProviderChunk(kind="text", text="e4 grabs the center.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_echo_verifier,
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    assert not [
        e for e in events if e.kind == "ai_tool_call_failed"
        and e.payload.get("error") == "compare_first"
    ]
    recs = [e for e in events if e.kind == "ai_recommendation"]
    assert recs and recs[-1].payload.get("uci") == "e2e4"


@pytest.mark.asyncio
async def test_accepted_recommend_drives_the_arrow():
    # The accepted recommend_move move is the one the arrow (ai_recommendation)
    # shows -- a single call, no second attempt needed.
    async def recommend(_input, *, cancel_token):
        board = chess.Board()
        return {"ok": True, "uci": board.parse_san(_input["move"]).uci()}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, _RecordingScriptedProvider(rounds=[
            [ProviderChunk(
                kind="tool_use", tool_use_id="r0",
                tool_name="recommend_move", tool_input={"move": "Nf3"},
            )],
            [ProviderChunk(kind="text", text="Nf3 develops with tempo.")],
        ]),
        registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_echo_verifier,
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    recs = [e for e in events if e.kind == "ai_recommendation"]
    assert recs and recs[-1].payload.get("uci") == "g1f3"


@pytest.mark.asyncio
async def test_rejected_recommend_resolves_in_one_resubmit():
    # A rejected move names the stronger move; submitting THAT resolves the
    # turn in a single resubmit -- no open-ended probing of other moves.
    async def recommend(_input, *, cancel_token):
        board = chess.Board()
        uci = board.parse_san(_input["move"]).uci()
        if uci == "e2e4":
            return {"error": "recommendation_rejected",
                    "reason": "A stronger move is available: Nf3."}
        return {"ok": True, "uci": uci}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, _RecordingScriptedProvider(rounds=[
            [ProviderChunk(                                    # r0: e4 -> rejected, names Nf3
                kind="tool_use", tool_use_id="r0",
                tool_name="recommend_move", tool_input={"move": "e4"},
            )],
            [ProviderChunk(                                    # r1 (nudged): submit Nf3 -> ok
                kind="tool_use", tool_use_id="r1",
                tool_name="recommend_move", tool_input={"move": "Nf3"},
            )],
            [ProviderChunk(kind="text", text="Nf3 it is.")],   # r2: conclusion
        ]),
        registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_echo_verifier,
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    recs = [e for e in events if e.kind == "ai_recommendation"]
    assert recs and recs[-1].payload.get("uci") == "g1f3"
    assert not events[-1].payload.get("no_recommendation")


@pytest.mark.asyncio
async def test_repeated_recommend_failures_force_top_moves_nudge():
    # Screenshot regression: the model guesses illegal moves one at a time
    # via recommend_move. After MAX_RECOMMEND_FAILURES (2) failures, the loop
    # injects a user nudge telling it to use top_moves instead.
    from sturddle_view.play.ai_analysis import _RECOMMEND_FAILURE_NUDGE

    async def recommend(_input, *, cancel_token):
        # First two attempts fail; the third (after the nudge) is accepted so
        # the turn ends cleanly.
        if _input["move"] in ("Nd7", "Ne7"):
            return {"error": "illegal_move", "detail": "illegal", "legal_moves": ["Nf3"]}
        return {"ok": True, "uci": chess.Board().parse_san(_input["move"]).uci()}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(kind="tool_use", tool_use_id="r0",     # failure 1
                       tool_name="recommend_move", tool_input={"move": "Nd7"})],
        [ProviderChunk(kind="tool_use", tool_use_id="r1",     # failure 2 -> nudge after
                       tool_name="recommend_move", tool_input={"move": "Ne7"})],
        [ProviderChunk(kind="tool_use", tool_use_id="r2",     # r2: nudged -> accepts Nf3
                       tool_name="recommend_move", tool_input={"move": "Nf3"})],
        [ProviderChunk(kind="text", text="Develops the knight.")],  # r3: conclusion, clean exit
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: chess.Board()),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # The nudge user message is in the history the r2 round was sent.
    r2_msgs = provider.calls[2]["messages"]
    assert any(
        m["role"] == "user" and m["content"] == _RECOMMEND_FAILURE_NUDGE
        for m in r2_msgs
    ), "top_moves nudge should fire after 2 failed recommend_move calls"


@pytest.mark.asyncio
async def test_top_moves_call_rearms_the_failure_nudge():
    # A single failure does NOT trip the nudge (cap is 2). The nudge stays
    # un-fired so long as the model never accumulates 2 failures in a row.
    from sturddle_view.play.ai_analysis import _RECOMMEND_FAILURE_NUDGE

    async def recommend(_input, *, cancel_token):
        if _input["move"] in ("Nd7", "Ne7"):
            return {"error": "illegal_move", "detail": "illegal", "legal_moves": ["Nf3"]}
        return {"ok": True, "uci": chess.Board().parse_san(_input["move"]).uci()}

    async def top_moves(_input, *, cancel_token):
        return {"candidates": [{"move_uci": "g1f3", "move_san": "Nf3", "score_cp": 20}]}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    reg.register(
        ToolSpec(name="top_moves", description="rank", input_schema={"type": "object"}),
        top_moves,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(kind="tool_use", tool_use_id="r0",     # failure 1
                       tool_name="recommend_move", tool_input={"move": "Nd7"})],
        [ProviderChunk(kind="tool_use", tool_use_id="t0",     # top_moves -> resets streak
                       tool_name="top_moves", tool_input={"moves": ["Nf3"]})],
        [ProviderChunk(kind="tool_use", tool_use_id="r1",     # failure 1 again (streak reset)
                       tool_name="recommend_move", tool_input={"move": "Ne7"})],
        [ProviderChunk(kind="tool_use", tool_use_id="r2",     # r3: accept, end the turn
                       tool_name="recommend_move", tool_input={"move": "Nf3"})],
        [ProviderChunk(kind="text", text="Develops the knight.")],  # r4: conclusion, clean exit
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: chess.Board()),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # top_moves reset the streak, so the lone failure on each side of it
    # never reaches the cap -- the nudge never fired.
    all_user_content = [
        m.get("content")
        for c in provider.calls for m in c["messages"]
        if m.get("role") == "user"
    ]
    assert _RECOMMEND_FAILURE_NUDGE not in all_user_content


@pytest.mark.asyncio
async def test_verifier_concluding_without_tool_gets_one_nudge():
    # The verifier must check before concluding. A verdict-from-intuition
    # (no tool call) draws exactly one tool nudge, then it complies.
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("d1", "check e4")],                # narrator
        [ProviderChunk(kind="text", text="e4 looks fine.")],  # verifier: no tool -> nudged
        [ProviderChunk(                                     # verifier: now calls a tool
            kind="tool_use", tool_use_id="v1",
            tool_name="piece_at", tool_input={"square": "e2"},
        )],
        [ProviderChunk(kind="text", text="e4 is sound.")],  # verifier: verdict
        [ProviderChunk(kind="text", text="done.")],         # narrator
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider)

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # The verifier's first round drew a nudge: the next verifier round's
    # last user message is the tool nudge (not the delegated question).
    verifier_rounds = [
        c for c in provider.calls if _VERIFIER_PROMPT_MARKER in c["system"]
    ]
    nudged = verifier_rounds[1]
    last_user = next(
        m for m in reversed(nudged["messages"]) if m["role"] == "user"
    )
    assert "tool" in last_user["content"].lower()


@pytest.mark.asyncio
async def test_delegate_blank_question_returns_error_without_subrun():
    # A blank question is rejected by the delegate tool before any verifier
    # sub-run; the narrator gets a structured error tool_result.
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("d1", "   ")],                     # narrator: blank question
        [ProviderChunk(kind="text", text="never mind.")],  # narrator: recovers
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider)

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # No verifier sub-run happened (only the two narrator rounds).
    assert all(_VERIFIER_PROMPT_MARKER not in c["system"] for c in provider.calls)
    # The delegate tool_result carries the structured error.
    tool_result = next(
        b
        for c in provider.calls
        for m in c["messages"] if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    assert "invalid_input" in tool_result["content"]


@pytest.mark.asyncio
async def test_two_delegates_each_get_their_own_parent_id():
    # _active_delegate_id is mutable coordinator state; a second delegate
    # must stamp its verifier's events with its OWN id, no stale leakage.
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("dA", "check e4")],               # narrator: delegate A
        [ProviderChunk(                                    # verifier A: tool
            kind="tool_use", tool_use_id="vA",
            tool_name="piece_at", tool_input={"square": "e2"},
        )],
        [ProviderChunk(kind="text", text="e4 sound.")],    # verifier A: verdict
        [_delegate_chunk("dB", "check d4")],               # narrator: delegate B
        [ProviderChunk(                                    # verifier B: tool
            kind="tool_use", tool_use_id="vB",
            tool_name="piece_at", tool_input={"square": "d2"},
        )],
        [ProviderChunk(kind="text", text="d4 sound.")],    # verifier B: verdict
        [ProviderChunk(kind="text", text="done.")],        # narrator
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider)

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    parents = {
        e.payload.get("tool_use_id"): e.payload.get("parent_tool_use_id")
        for e in events
        if e.kind == "ai_tool_call" and e.payload.get("name") == "piece_at"
    }
    assert parents == {"vA": "dA", "vB": "dB"}


@pytest.mark.asyncio
async def test_silent_round_after_prose_ends_turn_without_renudging():
    # Completeness nudge ends the turn on a silent round rather than
    # re-nudging it (nudging an empty round would build an invalid
    # empty-assistant message, and a silent model won't comply). The model
    # produced prose first (so it's no_recommendation, not no_response),
    # then went silent without ever attempting recommend_move.
    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        _noop_tool,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="The center is contested.")],  # r0: prose -> nudge #1
        [],                                                # r1: silent -> end turn
        [ProviderChunk(kind="text", text="should never run.")],  # guard vs looping
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: None),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    # r0 prose -> nudge #1 -> r1 silent: the empty round ends the turn (no
    # re-nudge, no loop). Exactly 2 rounds; flagged no_recommendation.
    assert len(provider.calls) == 2
    done = events[-1]
    assert done.payload.get("no_recommendation") is True


@pytest.mark.asyncio
async def test_whitespace_only_turn_flags_no_response_not_no_recommendation():
    # A turn whose only text is whitespace produced no real answer: it must
    # read as no_response, not no_recommendation (text_published gates on a
    # non-whitespace char, matching _round_produced_output's strip).
    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        _noop_tool,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="   \n  ")],  # whitespace only -> silent
        [ProviderChunk(kind="text", text="should never run.")],  # guard vs loop
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: None),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    assert len(provider.calls) == 1
    done = events[-1]
    assert done.payload.get("no_response") is True
    assert not done.payload.get("no_recommendation")


def test_empty_round_assistant_message_uses_placeholder():
    # An otherwise-empty assistant turn (e.g. the post-recommend nudge
    # firing on a silent round) must carry a non-empty content block so the
    # wire stays valid -- Anthropic rejects empty assistant content.
    msg = _assistant_message([ProviderChunk(kind="text", text="   ")])
    assert msg["role"] == "assistant"
    assert msg["content"] == [{"type": "text", "text": _EMPTY_TURN_PLACEHOLDER}]
    # A real text chunk is preserved verbatim (no placeholder).
    msg2 = _assistant_message([ProviderChunk(kind="text", text="real prose")])
    assert msg2["content"] == [{"type": "text", "text": "real prose"}]


def test_thinking_is_not_fed_back_into_assistant_message():
    # Thinking must NOT round-trip: we lack the signature Anthropic needs
    # on a returned thinking block, and reasoning-as-text is wrong shape.
    # Thinking alongside real text: only the text survives.
    msg = _assistant_message([
        ProviderChunk(kind="thinking", text="let me reason about this"),
        ProviderChunk(kind="text", text="the conclusion"),
    ])
    assert msg["content"] == [{"type": "text", "text": "the conclusion"}]
    # Thinking-only round has no fed-back content -> placeholder.
    msg2 = _assistant_message([ProviderChunk(kind="thinking", text="thinking only")])
    assert msg2["content"] == [{"type": "text", "text": _EMPTY_TURN_PLACEHOLDER}]
    # Thinking before a tool_use: thinking dropped, tool_use survives.
    msg3 = _assistant_message([
        ProviderChunk(kind="thinking", text="hmm"),
        ProviderChunk(kind="tool_use", tool_use_id="t1", tool_name="analyze",
                      tool_input={"fen": "startpos"}),
    ])
    assert msg3["content"] == [
        {"type": "tool_use", "id": "t1", "name": "analyze", "input": {"fen": "startpos"}},
    ]


def test_tool_signature_copied_onto_tool_use_block():
    # A tool_use chunk carrying a provider signature (Gemini's
    # thought_signature) must land on the assistant block so the provider
    # can echo it back next round; absence leaves the block untouched.
    msg = _assistant_message([
        ProviderChunk(kind="tool_use", tool_use_id="t1", tool_name="analyze",
                      tool_input={"fen": "startpos"}, tool_signature="SIG_xyz"),
    ])
    assert msg["content"] == [{
        "type": "tool_use", "id": "t1", "name": "analyze",
        "input": {"fen": "startpos"}, TOOL_SIGNATURE_KEY: "SIG_xyz",
    }]
    # No signature -> no key (Anthropic/Ollama tool calls stay clean).
    msg2 = _assistant_message([
        ProviderChunk(kind="tool_use", tool_use_id="t2", tool_name="analyze",
                      tool_input={}),
    ])
    assert TOOL_SIGNATURE_KEY not in msg2["content"][0]
