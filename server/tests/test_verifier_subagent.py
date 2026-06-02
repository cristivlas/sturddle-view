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


# The alternative-examined gate holds an accepted recommend_move until the
# model has looked at a different move this turn. Tests that exercise the
# accept path register this stub and play `_examine_round` first to clear
# the gate without standing up a real verifier sub-run.
async def _stub_top_moves(_input, *, cancel_token):
    return {"candidates": [{"move_uci": "b1c3"}, {"move_uci": "g1f3"}]}


def _register_top_moves(reg: ToolRegistry) -> None:
    reg.register(
        ToolSpec(name="top_moves", description="rank", input_schema={"type": "object"}),
        _stub_top_moves,
    )


def _examine_round(tool_use_id: str) -> list:
    """A scripted round that calls top_moves -- examines an alternative so
    the gate accepts the subsequent recommend_move."""
    return [ProviderChunk(
        kind="tool_use", tool_use_id=tool_use_id,
        tool_name="top_moves", tool_input={"moves": ["Nc3", "Nf3"]},
    )]


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
async def test_committed_prose_validates_against_live_board_only():
    # Commentator mode, a square reused by a later piece: a pawn lived on
    # b2 for most of the game, then was captured and the queen ended up on
    # b2. The history walk would excuse "pawn on b2", but the closing
    # post-recommendation plan must validate against the live board, where
    # b2 holds the queen -- so the false claim draws a corrective.
    # Immortal Game through 17...Qxb2: a White pawn lived on b2 for most
    # of the game, then the black queen captured it and now sits there.
    board = chess.Board()
    for san in (
        "e4 e5 f4 exf4 Bc4 Qh4+ Kf1 b5 Bxb5 Nf6 Nf3 Qh6 d3 Nh5 Nh4 Qg5 "
        "Nf5 c6 g4 Nf6 Rg1 cxb5 h4 Qg6 h5 Qg5 Qf3 Ng8 Bxf4 Qf6 Nc3 Bc5 "
        "Nd5 Qxb2"
    ).split():
        board.push_san(san)
    assert board.piece_at(chess.B2) is not None  # queen now on b2
    assert board.piece_at(chess.B2).piece_type == chess.QUEEN

    async def recommend(_input, *, cancel_token):
        # Echo the requested move so the gate sees distinct ucis.
        return {"ok": True, "uci": board.parse_san(_input["move"]).uci()}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )

    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(                                       # round 0: commit Nd4 -> gate blocks
            kind="tool_use", tool_use_id="r0",
            tool_name="recommend_move", tool_input={"move": "Nd4"},
        )],
        [ProviderChunk(                                       # round 1: accept a3
            kind="tool_use", tool_use_id="r1",
            tool_name="recommend_move", tool_input={"move": "a3"},
        )],
        [ProviderChunk(                                       # round 2: false closing claim
            kind="text",
            text="Capturing the pawn on b2 wins material for Black.",
        )],
        [ProviderChunk(kind="text", text="The queen on b2 stays active.")],  # round 3: rewrite
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: board),
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    correctives = [e for e in events if e.kind == "ai_corrective"]
    assert correctives, "committed prose should validate against the live board"
    assert "pawn on b2" in correctives[0].payload["false_claims"]


@pytest.mark.asyncio
async def test_wrong_side_to_move_attribution_flagged():
    # Black to move, but the prose credits the move to White: "White plays
    # Nd3" when Nd3 is Black's move (and no white knight reaches d3). The
    # attribution validator flags it via an ai_corrective.
    board = chess.Board("r2qr1k1/5ppp/p4n2/1pbP1bB1/1n6/N1N2B2/PP1Q1PPP/3R1RK1 b - - 1 16")
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="White plays Nd3, seizing the center.")],
        [ProviderChunk(kind="text", text="Black's knight eyes d3.")],  # rewrite
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(), board_provider=(lambda: board),
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    correctives = [e for e in events if e.kind == "ai_corrective"]
    assert correctives, "wrong-side attribution should draw a corrective"
    assert "White Nd3" in correctives[0].payload["attribution_errors"]


@pytest.mark.asyncio
async def test_post_recommend_nudge_fires_when_no_conclusion():
    # Move accepted, then the model exits with no prose: the nudge runs
    # one more round to extract the conclusion.
    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        _recommend_move_handler,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(                                       # round 0: commit Nf3 -> gate blocks
            kind="tool_use", tool_use_id="r0",
            tool_name="recommend_move", tool_input={"move": "Nf3"},
        )],
        [ProviderChunk(                                       # round 1: accept e4, no prose
            kind="tool_use", tool_use_id="r1",
            tool_name="recommend_move", tool_input={"move": "e4"},
        )],
        [],                                                   # round 2: still no prose -> nudge
        [ProviderChunk(kind="text", text="The plan is to control e5.")],  # round 3: conclusion
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: None),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # Round 1 accepts the move; round 2 exits with no conclusion -> the
    # nudge injects a prompt and runs round 3. The nudge's user message
    # (last user message before round 3's stream) asks for the conclusion.
    assert len(provider.calls) == 4
    nudge_round = provider.calls[3]
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
    _register_top_moves(reg)
    provider = _RecordingScriptedProvider(rounds=[
        _examine_round("t0"),                                 # round 0: clear the gate
        [                                                     # round 1: conclusion + move
            ProviderChunk(kind="text", text="e4 grabs the center; committing."),
            ProviderChunk(
                kind="tool_use", tool_use_id="r1",
                tool_name="recommend_move", tool_input={"move": "e4"},
            ),
        ],
        [],                                                   # round 2: model stops, no prose
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: None),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    await _drain_until_done(queue)

    # A round always follows the accepting call (the call ends round 1).
    # The same-round conclusion sets prose_after_recommend, so round 2's
    # empty exit does NOT trigger the nudge: exactly 3 rounds, no 4th.
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_rejected_recommend_is_renudged_until_accepted():
    # First attempt rejected; the completeness nudge re-fires (not one-shot)
    # and the model's second, accepted attempt resolves the turn.
    calls = {"n": 0}

    async def recommend(_input, *, cancel_token):
        calls["n"] += 1
        # r0 commits Nc3 (gate-blocked, records the alternative); r1 attempt
        # is engine-rejected; r2 attempt is accepted by the engine.
        board = chess.Board()
        uci = board.parse_san(_input["move"]).uci()
        if calls["n"] == 2:
            return {"error": "recommendation_rejected", "reason": "Engine prefers Nf3."}
        return {"ok": True, "uci": uci}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        recommend,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(                                    # r0: commit Nc3 -> gate blocks
            kind="tool_use", tool_use_id="r0",
            tool_name="recommend_move", tool_input={"move": "Nc3"},
        )],
        [ProviderChunk(                                    # r1: attempt e4 -> engine-rejected
            kind="tool_use", tool_use_id="r1",
            tool_name="recommend_move", tool_input={"move": "e4"},
        )],
        [ProviderChunk(                                    # r2 (nudged): second attempt
            kind="tool_use", tool_use_id="r2",
            tool_name="recommend_move", tool_input={"move": "Nf3"},
        )],
        [ProviderChunk(kind="text", text="Developing toward the center.")],  # r3: conclusion
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: None),
    )

    await coord.run(game_id="g", mode="coach", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    # r0's commit is gate-blocked (no prior recommend) but records Nc3 as an
    # examined alternative; r1's attempt is engine-rejected and re-nudged;
    # r2's attempt clears both the engine check and the alternative gate
    # (Nc3 committed in r0), so the turn ends cleanly.
    assert calls["n"] == 3
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


# --- Alternative-examined gate ---------------------------------------------
# The gate holds an otherwise-accepted recommend_move until a PRIOR
# recommend_move this turn committed a move OTHER than the one now being
# committed. Only recommend_move counts -- examining via delegate or
# top_moves does not clear the gate (draconian basis: the model must have
# concretely committed an alternative, not merely looked at one).


def _gate_reg():
    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="recommend_move", description="rec", input_schema={"type": "object"}),
        _recommend_move_handler,
    )
    return reg


async def _recommend_move_handler(_input, *, cancel_token):
    # Echo the requested move as UCI so distinct recommend_move calls yield
    # distinct ucis -- the gate now clears only on a prior recommend_move of
    # a DIFFERENT move.
    board = chess.Board()
    move = board.parse_san(_input["move"])
    return {"ok": True, "uci": move.uci()}


@pytest.mark.asyncio
async def test_gate_blocks_in_turn_then_falls_back_to_unvetted():
    # Straight to recommend_move, nothing else examined: the gate blocks the
    # in-turn commit (alternative_required), and when the model stalls the
    # turn falls back to the blocked move flagged unvetted -- not nothing.
    reg = _gate_reg()
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(                                    # r0: commit, gate blocks
            kind="tool_use", tool_use_id="r1",
            tool_name="recommend_move", tool_input={"move": "e4"},
        )],
        [ProviderChunk(kind="text", text="No alternative, sticking with it.")],  # r1: stall
        [ProviderChunk(kind="text", text="Still nothing.")],  # r2: stall -> give up
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_accept_verifier,
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    failures = [e for e in events if e.kind == "ai_tool_call_failed"]
    assert any(e.payload.get("error") == "alternative_required" for e in failures)
    # Fallback: the blocked move is surfaced, flagged unvetted, not withheld.
    recs = [e for e in events if e.kind == "ai_recommendation"]
    assert recs and recs[-1].payload.get("unvetted") is True
    assert events[-1].payload.get("unvetted") is True
    assert not events[-1].payload.get("no_recommendation")


@pytest.mark.asyncio
async def test_gate_accepts_after_recommending_a_different_move():
    # A prior recommend_move on a move OTHER than the one committed clears
    # the gate: the first commit (Nf3) is blocked but recorded, then the
    # second commit (e4) goes through and the recommendation fires.
    reg = _gate_reg()
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, _RecordingScriptedProvider(rounds=[
            [ProviderChunk(                                         # r0: commit Nf3 -> blocked
                kind="tool_use", tool_use_id="r0",
                tool_name="recommend_move", tool_input={"move": "Nf3"},
            )],
            [ProviderChunk(                                         # r1: commit e4 -> allowed
                kind="tool_use", tool_use_id="r1",
                tool_name="recommend_move", tool_input={"move": "e4"},
            )],
            [ProviderChunk(kind="text", text="e4 grabs the center.")],  # r2: conclusion
        ]),
        registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_accept_verifier,
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    # r0 (the alternative) is blocked; r1 (the real commit) clears -- so only
    # the first call carries alternative_required, and a recommendation fires.
    gated = [
        e for e in events if e.kind == "ai_tool_call_failed"
        and e.payload.get("error") == "alternative_required"
    ]
    assert [e.payload["tool_use_id"] for e in gated] == ["r0"]
    assert [e for e in events if e.kind == "ai_recommendation"]


@pytest.mark.asyncio
async def test_commentator_gate_blocks_endorsing_played_move_without_alternative():
    # played_uci seeds the gate: endorsing the played move (e4) on the first
    # recommend is blocked, because the played move is the subject under
    # review, not an alternative to it.
    reg = _gate_reg()
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, _RecordingScriptedProvider(rounds=[
            [ProviderChunk(                                       # r0: endorse e4 -> blocked
                kind="tool_use", tool_use_id="r0",
                tool_name="recommend_move", tool_input={"move": "e4"},
            )],
            [ProviderChunk(kind="text", text="Sticking with e4.")],  # r1: stall
            [ProviderChunk(kind="text", text="Nothing new.")],       # r2: stall -> give up
        ]),
        registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_accept_verifier,
    )

    await coord.run(
        game_id="g", mode="commentator",
        user_message=_TURN_CONTEXT + "\n", played_uci="e2e4",
    )
    events = await _drain_until_done(queue)

    failures = [e for e in events if e.kind == "ai_tool_call_failed"]
    assert any(e.payload.get("error") == "alternative_required" for e in failures)


@pytest.mark.asyncio
async def test_commentator_gate_clears_when_alternative_to_played_move_recommended():
    # With played_uci=e2e4, recommending a DIFFERENT move (Nf3) first clears
    # the gate, so a later endorsement of the played move (e4) goes through.
    reg = _gate_reg()
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, _RecordingScriptedProvider(rounds=[
            [ProviderChunk(                                       # r0: recommend Nf3 (alt) -> clears
                kind="tool_use", tool_use_id="r0",
                tool_name="recommend_move", tool_input={"move": "Nf3"},
            )],
            [ProviderChunk(                                       # r1: endorse e4 -> allowed
                kind="tool_use", tool_use_id="r1",
                tool_name="recommend_move", tool_input={"move": "e4"},
            )],
            [ProviderChunk(kind="text", text="e4 was best after all.")],  # r2: conclusion
        ]),
        registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_accept_verifier,
    )

    await coord.run(
        game_id="g", mode="commentator",
        user_message=_TURN_CONTEXT + "\n", played_uci="e2e4",
    )
    events = await _drain_until_done(queue)

    # Nf3 (r0) clears against the seeded played move immediately, so neither
    # call is gated and a recommendation fires.
    assert not [
        e for e in events if e.kind == "ai_tool_call_failed"
        and e.payload.get("error") == "alternative_required"
    ]
    assert [e for e in events if e.kind == "ai_recommendation"]


@pytest.mark.asyncio
async def test_final_recommend_of_played_move_wins_the_arrow():
    # Screenshot regression: model tests the played move (e4) first -- gated,
    # but recorded -- then an alternative (Nf3) which clears, then concludes
    # the played move was best and re-recommends it. The final recommendation
    # (the arrow) must be the played move, not the alternative that merely
    # cleared the gate first.
    reg = _gate_reg()
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, _RecordingScriptedProvider(rounds=[
            [ProviderChunk(                                       # r0: e4 (played) -> gated
                kind="tool_use", tool_use_id="r0",
                tool_name="recommend_move", tool_input={"move": "e4"},
            )],
            [ProviderChunk(                                       # r1: Nf3 (alt) -> clears
                kind="tool_use", tool_use_id="r1",
                tool_name="recommend_move", tool_input={"move": "Nf3"},
            )],
            [ProviderChunk(                                       # r2: re-commit e4 -> clears
                kind="tool_use", tool_use_id="r2",
                tool_name="recommend_move", tool_input={"move": "e4"},
            )],
            [ProviderChunk(kind="text", text="e4 is the stronger choice.")],  # r3: conclusion
        ]),
        registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_accept_verifier,
    )

    await coord.run(
        game_id="g", mode="commentator",
        user_message=_TURN_CONTEXT + "\n", played_uci="e2e4",
    )
    events = await _drain_until_done(queue)

    recs = [e for e in events if e.kind == "ai_recommendation"]
    assert recs and recs[-1].payload.get("uci") == "e2e4"


@pytest.mark.asyncio
async def test_gate_rejects_when_only_the_committed_move_was_recommended():
    # Recommending the SAME move twice is not examining an alternative --
    # the gate blocks every attempt and the turn never clears.
    reg = _gate_reg()
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, _RecordingScriptedProvider(rounds=[
            [ProviderChunk(                                       # r0: commit e4 -> blocked
                kind="tool_use", tool_use_id="r0",
                tool_name="recommend_move", tool_input={"move": "e4"},
            )],
            [ProviderChunk(                                       # r1: commit e4 again -> blocked
                kind="tool_use", tool_use_id="r1",
                tool_name="recommend_move", tool_input={"move": "e4"},
            )],
            [ProviderChunk(kind="text", text="Sticking with e4.")],  # r2: stall
            [ProviderChunk(kind="text", text="Nothing new.")],       # r3: stall -> give up
        ]),
        registry=reg, board_provider=(lambda: chess.Board()),
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    failures = [e for e in events if e.kind == "ai_tool_call_failed"]
    assert any(e.payload.get("error") == "alternative_required" for e in failures)


@pytest.mark.asyncio
async def test_gate_not_cleared_by_top_moves():
    # top_moves does NOT clear the gate (draconian: only a prior
    # recommend_move counts). Ranking Nc3/Nf3 then committing e4 is still
    # blocked.
    reg = _gate_reg()
    _register_top_moves(reg)
    provider = _RecordingScriptedProvider(rounds=[
        _examine_round("t0"),                              # r0: top_moves (Nc3, Nf3)
        [ProviderChunk(                                    # r1: commit e4 -> still blocked
            kind="tool_use", tool_use_id="r1",
            tool_name="recommend_move", tool_input={"move": "e4"},
        )],
        [ProviderChunk(kind="text", text="Sticking with e4.")],  # r2: stall
        [ProviderChunk(kind="text", text="Nothing new.")],       # r3: stall -> give up
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: chess.Board()),
        recommend_verifier=_accept_verifier,
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    failures = [e for e in events if e.kind == "ai_tool_call_failed"]
    assert any(e.payload.get("error") == "alternative_required" for e in failures)


async def _accept_verifier(move, depth, cancel_token):
    # Stand-in end-of-turn verifier: echoes the move so an ai_recommendation
    # event fires when the gate accepts.
    return {"uci": move.uci(), "san": "e4"}


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


# --- extra-board validation: prose moves in an examined position --------
_PROJ_KNIGHT_ON_F4 = "rnbqkb1r/pppppppp/8/8/5N2/8/PPPPPPPP/RNBQKB1R w KQkq - 0 1"


async def _stub_analyze(input_, *, cancel_token):
    # Body is irrelevant: the coordinator reads the examined FEN from the
    # call's `fen` input, not the result.
    return {"score_cp": 20, "depth": 20}


@pytest.mark.asyncio
async def test_prose_move_legal_only_in_examined_fen_not_flagged():
    # Live board is the start position, where Nd5 is illegal. The model
    # examines a projected position (knight on f4) via analyze, then names
    # Nd5 in prose -- legal there, so no illegal-move corrective fires.
    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="analyze", description="search", input_schema={"type": "object"}),
        _stub_analyze,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(                                    # r0: examine projected fen
            kind="tool_use", tool_use_id="a1",
            tool_name="analyze", tool_input={"fen": _PROJ_KNIGHT_ON_F4},
        )],
        [ProviderChunk(kind="text", text="The knight swings to d5: Nd5.")],  # r1: projected move
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: chess.Board()),
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    correctives = [e for e in events if e.kind == "ai_corrective"]
    assert not correctives, [e.payload for e in correctives]


@pytest.mark.asyncio
async def test_prose_move_illegal_everywhere_still_flagged():
    # No analyze call: Nd5 is illegal on the live start board and examined
    # nowhere, so the illegal-move corrective fires (guard against the
    # extra-board change silencing real hallucinations).
    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="analyze", description="search", input_schema={"type": "object"}),
        _stub_analyze,
    )
    provider = _RecordingScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="The knight swings to d5: Nd5.")],  # r0: live-illegal
        [ProviderChunk(kind="text", text="Rewriting.")],                     # r1: corrective round
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=(lambda: chess.Board()),
    )

    await coord.run(game_id="g", mode="commentator", user_message=_TURN_CONTEXT + "\n")
    events = await _drain_until_done(queue)

    correctives = [e for e in events if e.kind == "ai_corrective"]
    assert any("Nd5" in c.payload.get("illegal_moves", []) for c in correctives)
