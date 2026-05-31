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
    ToolRegistry,
    ToolSpec,
)
from sturddle_view.llm.base import LLMProvider, ToolWireSpec
from sturddle_view.llm.prompts import VERIFIER_ADDENDUM
from sturddle_view.play.ai_analysis import (
    AIAnalysisCoordinator,
    DELEGATE_TOOL_SPEC,
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
    reg = ToolRegistry()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg,
        board_provider=(lambda: board),
        verifier_registry=_verifier_registry(),
    )
    reg.register(DELEGATE_TOOL_SPEC, make_delegate_tool(coord.delegate_runner()))
    return coord


async def _drain_until_done(queue: asyncio.Queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


def _delegate_chunk(tool_use_id: str, question: str) -> ProviderChunk:
    return ProviderChunk(
        kind="tool_use",
        tool_use_id=tool_use_id,
        tool_name="delegate",
        tool_input={"question": question},
    )


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
        [ProviderChunk(                                       # round 0: accept move, no prose
            kind="tool_use", tool_use_id="r1",
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
            ProviderChunk(kind="text", text="e4 grabs the center; committing."),
            ProviderChunk(
                kind="tool_use", tool_use_id="r1",
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
async def test_verifier_line_internal_move_not_flagged():
    # The verifier may narrate a refutation line ("after exd4..."); that
    # token is illegal on the live board but is reasoning, not a live-move
    # hallucination. It must NOT draw a corrective or burn an extra round.
    board = chess.Board()  # startpos; "exd4" is illegal here
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("d1", "check e4")],               # narrator
        [ProviderChunk(                                    # verifier: tool
            kind="tool_use", tool_use_id="v1",
            tool_name="piece_at", tool_input={"square": "e2"},
        )],
        [ProviderChunk(                                    # verifier: verdict naming a line
            kind="text",
            text="e4 is sound; after exd4 the center holds.",
        )],
        [ProviderChunk(kind="text", text="done.")],        # narrator
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider, board=board)

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    # No corrective fired for the line-internal "exd4".
    assert not [e for e in events if e.kind == "ai_corrective"]
    # The verifier concluded in its 2nd round (tool, then verdict) -- no
    # extra correction round: exactly 4 stream calls total.
    assert len(provider.calls) == 4


@pytest.mark.asyncio
async def test_verifier_false_live_piece_claim_still_flagged():
    # Skipping illegal-move validation for the verifier must NOT disable
    # the piece-claim guard: a fabricated live-board piece still corrects.
    board = chess.Board()  # startpos: no knight on e4
    provider = _RecordingScriptedProvider(rounds=[
        [_delegate_chunk("d1", "check e4")],               # narrator
        [ProviderChunk(                                    # verifier: tool
            kind="tool_use", tool_use_id="v1",
            tool_name="piece_at", tool_input={"square": "e2"},
        )],
        [ProviderChunk(                                    # verifier: false live claim
            kind="text", text="The knight on e4 dominates.",
        )],
        [ProviderChunk(kind="text", text="e4 is sound.")],  # verifier: corrected verdict
        [ProviderChunk(kind="text", text="done.")],         # narrator
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = _coordinator(bus, provider, board=board)

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # Verifier correctives are suppressed from the UI (silent sub-run), so
    # the proof is in the message history: a corrective user message was
    # injected after the false claim, asking the verifier to rewrite.
    corrective_texts = [
        m["content"]
        for c in provider.calls
        for m in c["messages"]
        if m["role"] == "user" and isinstance(m["content"], str)
        and "Not on the live board" in m["content"]
    ]
    assert corrective_texts, "false live piece claim should still be flagged"
    assert any("e4" in t for t in corrective_texts)
