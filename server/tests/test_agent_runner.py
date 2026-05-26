"""Agent runner loop in AIAnalysisCoordinator.

The coordinator owns the multi-turn loop:
- Calls provider.stream(system, messages, tools) for one round.
- On a tool_use chunk, dispatches via the registry, captures the result,
  appends the assistant + tool_result messages, and loops.
- Stops when the round ends without a tool_use, when the round budget
  is exhausted, or when the user cancels.

Tests use ScriptedProvider as the provider double and a fake registry.
No HTTP, no real engine.
"""
from __future__ import annotations

import asyncio

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm import (
    ProviderChunk,
    ScriptedProvider,
    ToolRegistry,
    ToolSpec,
)
from sturddle_view.play.ai_analysis import (
    AIAnalysisCoordinator,
    ERROR_DETAIL_MAX_LEN,
)


def _make_registry(handlers: dict) -> ToolRegistry:
    """handlers: name -> async fn(input, *, cancel_token) -> dict."""
    reg = ToolRegistry()
    for name, fn in handlers.items():
        reg.register(
            ToolSpec(name=name, description=name, input_schema={"type": "object"}),
            fn,
        )
    return reg


def _payload_subset(payload: dict, expected: dict) -> bool:
    """Subset match -- payloads carry an auto-injected `seq` field, so
    strict equality is brittle. True iff every expected key/value pair
    appears in payload (extra keys allowed)."""
    return all(payload.get(k) == v for k, v in expected.items())


async def _drain_until_done(queue: asyncio.Queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


@pytest.mark.asyncio
async def test_single_round_no_tool_use_completes_normally():
    # Phase 0 behavior must still hold: provider yields text only, no
    # tool_use, runner streams to bus + emits terminal done.
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="Hello."),
    ]])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    deltas = [e.payload["delta"] for e in events if "delta" in e.payload]
    assert deltas == ["Hello."]
    assert _payload_subset(events[-1].payload, {"done": True})
    assert provider.stream_calls == 1


@pytest.mark.asyncio
async def test_tool_use_dispatches_and_feeds_result_into_next_round():
    captured: dict = {}

    async def fake_tool(payload, *, cancel_token):
        captured["input"] = dict(payload)
        captured["token_seen"] = cancel_token is not None
        return {"score_cp": 42}

    reg = _make_registry({"analyze": fake_tool})

    # Round 1: text + tool_use. Round 2: terminal text.
    provider = ScriptedProvider(rounds=[
        [
            ProviderChunk(kind="text", text="Thinking. "),
            ProviderChunk(
                kind="tool_use",
                tool_use_id="tu_1",
                tool_name="analyze",
                tool_input={"fen": "startpos"},
            ),
        ],
        [
            ProviderChunk(kind="text", text="It's +0.42."),
        ],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    # Tool got the input + a cancel token.
    assert captured["input"] == {"fen": "startpos"}
    assert captured["token_seen"] is True

    # Two rounds happened.
    assert provider.stream_calls == 2

    # Second round's messages must carry assistant tool_use + user
    # tool_result keyed by the same tool_use_id.
    snap = provider.last_call
    assert snap is not None
    # Expected shape:
    #  [user(initial), assistant([{type:text}, {type:tool_use,...}]), user([{type:tool_result,...}])]
    assert len(snap["messages"]) >= 3
    assistant = snap["messages"][-2]
    tool_result_msg = snap["messages"][-1]
    assert assistant["role"] == "assistant"
    assert any(
        b.get("type") == "tool_use" and b.get("id") == "tu_1"
        for b in assistant["content"]
    )
    assert tool_result_msg["role"] == "user"
    tr_block = tool_result_msg["content"][0]
    assert tr_block["type"] == "tool_result"
    assert tr_block["tool_use_id"] == "tu_1"

    # Prose emitted to the bus spans both rounds.
    deltas = [e.payload["delta"] for e in events if "delta" in e.payload]
    assert deltas == ["Thinking. ", "It's +0.42."]
    assert _payload_subset(events[-1].payload, {"done": True})


@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error_and_loop_continues():
    # Registry has no "missing" tool; runner must feed back a structured
    # error tool_result instead of crashing the turn, so the model can
    # recover or finish gracefully.
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use",
            tool_use_id="tu_x",
            tool_name="missing",
            tool_input={},
        )],
        [ProviderChunk(kind="text", text="ok, giving up.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert provider.stream_calls == 2
    snap = provider.last_call
    tr = snap["messages"][-1]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "tu_x"
    # The structured error is JSON-serialized into the tool_result content.
    assert "unknown_tool" in tr["content"]
    # Turn still completes with terminal done (no error marker since
    # the runner handled the error gracefully).
    assert _payload_subset(events[-1].payload, {"done": True})


@pytest.mark.asyncio
async def test_tool_raising_returns_structured_error_and_loop_continues():
    async def boom(_input, *, cancel_token):
        raise RuntimeError("kaboom")

    reg = _make_registry({"boom": boom})
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use",
            tool_use_id="tu_b",
            tool_name="boom",
            tool_input={},
        )],
        [ProviderChunk(kind="text", text="recovered.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert provider.stream_calls == 2
    tr = provider.last_call["messages"][-1]["content"][0]
    assert "tool_failed" in tr["content"]
    assert "kaboom" in tr["content"]
    assert _payload_subset(events[-1].payload, {"done": True})


@pytest.mark.asyncio
async def test_cancel_mid_tool_propagates_and_emits_cancelled_done():
    tool_started = asyncio.Event()

    async def hangs(_input, *, cancel_token):
        tool_started.set()
        # Wait on the token explicitly -- a cooperative tool would
        # short-circuit here. If cancellation never arrives this will
        # block forever, which makes the test fail loudly rather than
        # passing on the wrong code path.
        await cancel_token.wait_cancelled()
        raise asyncio.CancelledError()  # surface as cancellation

    reg = _make_registry({"slow": hangs})
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use",
            tool_use_id="tu_s",
            tool_name="slow",
            tool_input={},
        )],
        # Second round never runs because we cancel mid-tool.
        [ProviderChunk(kind="text", text="unreached")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    run_task = asyncio.create_task(coord.run(game_id="g"))
    # Wait until the tool is actually executing before cancelling, so
    # the test exercises the mid-tool path (not the "before any work"
    # path) deterministically without any timer.
    await tool_started.wait()

    await coord.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    # Drain bus: only the terminal cancelled event should be there
    # (the tool_use chunk doesn't produce an ai_info delta).
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events[-1].kind == "ai_info"
    assert _payload_subset(events[-1].payload, {"done": True, "cancelled": True})
    # Second round never happened.
    assert provider.stream_calls == 1


@pytest.mark.asyncio
async def test_round_cap_stops_runaway_loop():
    # Provider keeps asking for a tool forever. The runner must stop
    # at MAX_TOOL_ROUNDS rather than burning the whole script.
    async def echo(_input, *, cancel_token):
        return {"ok": True}

    reg = _make_registry({"loop": echo})

    # Generate enough rounds to exceed the cap. Each round only has a
    # tool_use; no text. The runner should consume exactly the cap and
    # stop.
    from sturddle_view.play.ai_analysis import MAX_TOOL_ROUNDS
    rounds = [
        [ProviderChunk(
            kind="tool_use",
            tool_use_id=f"tu_{i}",
            tool_name="loop",
            tool_input={},
        )]
        for i in range(MAX_TOOL_ROUNDS + 5)
    ]

    provider = ScriptedProvider(rounds=rounds)
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert provider.stream_calls == MAX_TOOL_ROUNDS
    # Terminal event carries `round_cap=True` so the UI can flag that
    # the turn stopped on the guardrail rather than reaching an answer.
    assert _payload_subset(events[-1].payload, {"done": True, "round_cap": True})


@pytest.mark.asyncio
async def test_tools_schema_is_passed_to_provider_each_round():
    async def t(p, *, cancel_token):
        return {"ok": True}

    reg = _make_registry({"a": t})

    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="done"),
    ]])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")

    snap = provider.last_call
    assert snap is not None
    assert snap["tools"] == reg.schemas()


@pytest.mark.asyncio
async def test_provider_error_publishes_done_with_error_kind_and_detail():
    """A provider raising (HTTP error, malformed wire) must surface a
    done event carrying BOTH the exception class name AND the message.
    Without the detail, the client only sees "RuntimeError" and the
    user has to dig through the transcript file to learn what went
    wrong (e.g. "model does not support tools")."""
    class _BoomProvider(ScriptedProvider):
        async def stream(self, system, messages, tools=None, *, transcript=None, round_index=0):
            raise RuntimeError("ollama API error 400: model does not support tools")
            yield  # pragma: no cover - marks this as an async generator

    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, _BoomProvider(rounds=[]))

    with pytest.raises(RuntimeError):
        await coord.run(game_id="g")

    events = await _drain_until_done(queue)
    terminal = events[-1].payload
    assert terminal["done"] is True
    assert terminal["error"] == "RuntimeError"
    # The message is what the client needs to show "API key invalid" /
    # "does not support tools" / etc. without making users tail a log.
    assert "does not support tools" in terminal["error_detail"]


@pytest.mark.asyncio
async def test_no_response_flagged_when_round_ends_without_text():
    """Some models stream chain-of-thought (`reasoning`) but never emit
    user-facing text. The runner must surface that so the UI can show
    "no answer" instead of a silent empty panel."""
    # Provider returns one round with NO text and NO tool_use -- the
    # natural-exit path with text_published=False.
    provider = ScriptedProvider(rounds=[[]])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    terminal = events[-1].payload
    assert terminal["done"] is True
    assert terminal.get("no_response") is True
    assert "round_cap" not in terminal  # natural exit, not the guardrail


@pytest.mark.asyncio
async def test_no_response_NOT_flagged_when_text_was_streamed():
    provider = ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="ok")]])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)
    assert "no_response" not in events[-1].payload


@pytest.mark.asyncio
async def test_thinking_chunks_publish_ai_thinking_events():
    """Reasoning-only models emit `thinking` ProviderChunks. The runner
    must republish them as `ai_thinking` events so the UI can render a
    collapsible disclosure -- without this they only land in the
    transcript file."""
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="thinking", text="let me think... "),
        ProviderChunk(kind="thinking", text="checking the position."),
        ProviderChunk(kind="text", text="OK, e4 is best."),
    ]])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    thinking = [e for e in events if e.kind == "ai_thinking"]
    info = [e for e in events if e.kind == "ai_info" and "delta" in e.payload]
    assert [e.payload["delta"] for e in thinking] == [
        "let me think... ",
        "checking the position.",
    ]
    assert [e.payload["delta"] for e in info] == ["OK, e4 is best."]
    for e in thinking:
        assert e.game_id == "g"


@pytest.mark.asyncio
async def test_empty_thinking_chunks_not_published():
    """Empty-text thinking chunks (which can happen on provider
    boundaries) must not generate stray ai_thinking events."""
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="thinking", text=""),
        ProviderChunk(kind="text", text="hi"),
    ]])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert [e for e in events if e.kind == "ai_thinking"] == []


@pytest.mark.asyncio
async def test_error_detail_truncated_to_cap():
    """A misbehaving provider could return a wall of HTML. The done
    payload caps the detail string so the event-bus payload stays
    small; full text is in the transcript anyway."""
    long_msg = "x" * (ERROR_DETAIL_MAX_LEN * 3)

    class _BigBoom(ScriptedProvider):
        async def stream(self, system, messages, tools=None, *, transcript=None, round_index=0):
            raise RuntimeError(long_msg)
            yield  # pragma: no cover

    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, _BigBoom(rounds=[]))

    with pytest.raises(RuntimeError):
        await coord.run(game_id="g")

    events = await _drain_until_done(queue)
    detail = events[-1].payload["error_detail"]
    assert len(detail) == ERROR_DETAIL_MAX_LEN


# ---------- Tool cards (lazy per-tool guidance) ------------------------
# See docs/ai-analysis-skills-spec.md. A tool's card is appended as a
# text content block inside the tool_result user message, on the first
# call to that tool per turn. Subsequent calls to the same tool reuse
# the message-list prefix (card already in context); no re-injection.


def _make_registry_with_card(name: str, fn, card: str | None) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name=name, description=name, input_schema={"type": "object"}, card=card,
        ),
        fn,
    )
    return reg


def _tool_result_user_msgs(snap_messages: list) -> list[dict]:
    """All user-role messages in a provider snapshot that carry a
    tool_result block (skips the opening user message)."""
    out: list[dict] = []
    for m in snap_messages:
        if m["role"] != "user" or not isinstance(m["content"], list):
            continue
        if any(b.get("type") == "tool_result" for b in m["content"]):
            out.append(m)
    return out


@pytest.mark.asyncio
async def test_card_injected_after_first_tool_call():
    async def ok(_input, *, cancel_token):
        return {"legal": True}

    reg = _make_registry_with_card("validate_move", ok, "USE_THE_CARD")
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="t1",
            tool_name="validate_move", tool_input={"move": "e4"},
        )],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")

    tr_msgs = _tool_result_user_msgs(provider.last_call["messages"])
    assert len(tr_msgs) == 1
    blocks = tr_msgs[0]["content"]
    # First block is the tool_result data; second is the card text.
    assert blocks[0]["type"] == "tool_result"
    assert blocks[1] == {"type": "text", "text": "USE_THE_CARD"}


@pytest.mark.asyncio
async def test_card_not_reinjected_on_repeat_calls():
    async def ok(_input, *, cancel_token):
        return {"legal": True}

    reg = _make_registry_with_card("validate_move", ok, "USE_THE_CARD")
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="t1",
            tool_name="validate_move", tool_input={"move": "e4"},
        )],
        [ProviderChunk(
            kind="tool_use", tool_use_id="t2",
            tool_name="validate_move", tool_input={"move": "d4"},
        )],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")

    tr_msgs = _tool_result_user_msgs(provider.last_call["messages"])
    assert len(tr_msgs) == 2
    # First tool_result message carries the card; second does not.
    assert any(b.get("type") == "text" and b.get("text") == "USE_THE_CARD"
               for b in tr_msgs[0]["content"])
    assert all(b.get("type") != "text" for b in tr_msgs[1]["content"])


@pytest.mark.asyncio
async def test_no_card_means_no_text_block():
    async def ok(_input, *, cancel_token):
        return {"ok": True}

    reg = _make_registry_with_card("analyze", ok, None)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="t1",
            tool_name="analyze", tool_input={"fen": "startpos"},
        )],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")

    tr_msgs = _tool_result_user_msgs(provider.last_call["messages"])
    assert len(tr_msgs) == 1
    # tool_result only, no card text block.
    blocks = tr_msgs[0]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_distinct_tools_each_inject_their_own_card():
    async def ok(_input, *, cancel_token):
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(
        ToolSpec(name="validate_move", description="vm",
                 input_schema={"type": "object"}, card="VM_CARD"),
        ok,
    )
    reg.register(
        ToolSpec(name="piece_at", description="pa",
                 input_schema={"type": "object"}, card="PA_CARD"),
        ok,
    )
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="t1",
            tool_name="validate_move", tool_input={"move": "e4"},
        )],
        [ProviderChunk(
            kind="tool_use", tool_use_id="t2",
            tool_name="piece_at", tool_input={"square": "e4"},
        )],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")

    tr_msgs = _tool_result_user_msgs(provider.last_call["messages"])
    assert len(tr_msgs) == 2
    texts = [
        b["text"] for m in tr_msgs for b in m["content"] if b.get("type") == "text"
    ]
    assert texts == ["VM_CARD", "PA_CARD"]


# ---------- Round-exit validators -------------------------------------
# Two parallel checks run when a round ends without a tool_use:
#   - illegal SAN-shaped tokens in the prose (move validator)
#   - false "piece on square" claims (piece-claim validator)
# On any hit, the coordinator injects a corrective user message and
# runs another round. Driven by the board_provider; absent provider
# disables both checks.


def _board_provider_for(board: chess.Board):
    return lambda: board


@pytest.mark.asyncio
async def test_validator_skipped_when_no_board_provider():
    # Without a board provider the round exits normally regardless of
    # what the model wrote -- back-compat for callers that don't wire it.
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="The move Qh9 wins."),  # nonsense move
    ]])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())

    await coord.run(game_id="g")

    assert provider.stream_calls == 1


@pytest.mark.asyncio
async def test_legal_moves_pass_validator():
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="Consider e4, then Nf3."),
    ]])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(),
        board_provider=_board_provider_for(chess.Board()),
    )

    await coord.run(game_id="g")

    assert provider.stream_calls == 1


@pytest.mark.asyncio
async def test_illegal_move_triggers_corrective_round():
    # "Nf6" is illegal for white on move 1 (knights can go Nf3, Nh3,
    # Nc3, Na3 -- not Nf6, which is a black-side square from f-file).
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="White should play Nf6 here.")],
        [ProviderChunk(kind="text", text="Revised: White should play Nf3.")],
    ])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(),
        board_provider=_board_provider_for(chess.Board()),
    )

    await coord.run(game_id="g")

    assert provider.stream_calls == 2
    # The corrective user message is the last message before the second
    # round's assistant content.
    snap_msgs = provider.last_call["messages"]
    corrective = snap_msgs[-1]
    assert corrective["role"] == "user"
    assert "Nf6" in corrective["content"]
    assert "do not exist" in corrective["content"]


@pytest.mark.asyncio
async def test_multiple_illegal_moves_listed_once_each():
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="Try Nf6 then Bd5. Or Nf6 again.")],
        [ProviderChunk(kind="text", text="Revised.")],
    ])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(),
        board_provider=_board_provider_for(chess.Board()),
    )

    await coord.run(game_id="g")

    snap_msgs = provider.last_call["messages"]
    corrective = snap_msgs[-1]  # last is the corrective user message
    # Both illegal moves listed, "Nf6" once (dedup).
    assert corrective["role"] == "user"
    assert corrective["content"].count("Nf6") == 1
    assert "Bd5" in corrective["content"]


@pytest.mark.asyncio
async def test_validator_only_runs_on_tool_use_free_exit():
    # When the round ends with a tool_use, the validator does not run --
    # the model's text may legitimately reference moves it is about to
    # verify with a tool call.
    async def ok(_input, *, cancel_token):
        return {"legal": False}

    reg = _make_registry({"validate_move": ok})
    provider = ScriptedProvider(rounds=[
        [
            ProviderChunk(kind="text", text="Trying Nf6."),
            ProviderChunk(
                kind="tool_use", tool_use_id="t1",
                tool_name="validate_move", tool_input={"move": "Nf6"},
            ),
        ],
        [ProviderChunk(kind="text", text="Got it, e4 instead.")],
    ])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg,
        board_provider=_board_provider_for(chess.Board()),
    )

    await coord.run(game_id="g")

    # 2 rounds, no extra corrective round triggered by round 1's text.
    assert provider.stream_calls == 2


@pytest.mark.asyncio
async def test_false_piece_claim_triggers_corrective_round():
    # Starting position: no piece on e4. Model invents one.
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="The bishop on e4 dominates.")],
        [ProviderChunk(kind="text", text="Revised, no bishop there.")],
    ])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(),
        board_provider=_board_provider_for(chess.Board()),
    )

    await coord.run(game_id="g")

    assert provider.stream_calls == 2
    corrective = provider.last_call["messages"][-1]
    assert corrective["role"] == "user"
    assert "bishop on e4" in corrective["content"]
    assert "False piece claim" in corrective["content"]


@pytest.mark.asyncio
async def test_illegal_move_and_false_piece_combined():
    # Both validator hits in one round -> single corrective with both.
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="text",
            text="The bishop on e4 supports Nf6.",  # bishop wrong; Nf6 illegal
        )],
        [ProviderChunk(kind="text", text="Revised.")],
    ])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(),
        board_provider=_board_provider_for(chess.Board()),
    )

    await coord.run(game_id="g")

    assert provider.stream_calls == 2
    corrective = provider.last_call["messages"][-1]
    assert "Nf6" in corrective["content"]
    assert "bishop on e4" in corrective["content"]


# ---------- Per-round event labeling for the UI -----------------------
# ai_info / ai_thinking carry the originating round_index so the panel
# can render rounds as separate sections. ai_tool_call surfaces tool
# dispatch. ai_corrective surfaces validator hits + the corrective
# round number.


@pytest.mark.asyncio
async def test_ai_info_payload_includes_round_index():
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="hello"),
    ]])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    deltas = [e for e in events if e.payload.get("delta")]
    assert all(e.payload.get("round") == 0 for e in deltas)


@pytest.mark.asyncio
async def test_ai_thinking_payload_includes_round_index():
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="thinking", text="hmm..."),
        ProviderChunk(kind="text", text="answer."),
    ]])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    thinking = [e for e in events if e.kind == "ai_thinking"]
    assert len(thinking) == 1
    assert thinking[0].payload["round"] == 0


@pytest.mark.asyncio
async def test_ai_tool_call_event_emitted_per_dispatch():
    async def ok(_input, *, cancel_token):
        return {"ok": True}

    reg = _make_registry({"analyze": ok})
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="t1",
            tool_name="analyze", tool_input={"fen": "startpos"},
        )],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    calls = [e for e in events if e.kind == "ai_tool_call"]
    assert len(calls) == 1
    assert _payload_subset(calls[0].payload, {
        "round": 0,
        "name": "analyze",
        "input": {"fen": "startpos"},
        "tool_use_id": "t1",
    })


@pytest.mark.asyncio
async def test_ai_corrective_event_emitted_on_validator_hit():
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="White plays Nf6.")],  # illegal
        [ProviderChunk(kind="text", text="revised: Nf3.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(),
        board_provider=_board_provider_for(chess.Board()),
    )

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    corrective = [e for e in events if e.kind == "ai_corrective"]
    assert len(corrective) == 1
    assert _payload_subset(corrective[0].payload, {
        "round": 1,  # the round that the corrective triggers
        "illegal_moves": ["Nf6"],
        "false_claims": [],
    })


@pytest.mark.asyncio
async def test_ai_tool_call_failed_event_on_tool_error():
    # Tool returns a structured {"error": ...} -- coordinator publishes
    # ai_tool_call_failed so the panel can mark the failing call.
    async def boom(_input, *, cancel_token):
        raise RuntimeError("kaboom")

    reg = _make_registry({"piece_at": boom})
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="t1",
            tool_name="piece_at", tool_input={"square": "e4"},
        )],
        [ProviderChunk(kind="text", text="ok.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    failed = [e for e in events if e.kind == "ai_tool_call_failed"]
    assert len(failed) == 1
    assert failed[0].payload["round"] == 0
    assert failed[0].payload["tool_use_id"] == "t1"
    assert failed[0].payload["error"] == "tool_failed"


@pytest.mark.asyncio
async def test_replay_buffer_captures_turn_events():
    # Replay returns the same events that flowed to the bus, in order.
    # Lets a reconnecting client rebuild the AI panel without missing
    # anything published during the WS gap.
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="hello"),
    ]])
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())

    await coord.run(game_id="g")
    replay = coord.replay()

    kinds = [e["kind"] for e in replay]
    assert "ai_info" in kinds
    # Terminal done is in the replay too.
    assert any(e["payload"].get("done") for e in replay)
    # Every event carries a monotonic seq.
    seqs = [e["payload"]["seq"] for e in replay]
    assert seqs == list(range(1, len(seqs) + 1))


@pytest.mark.asyncio
async def test_replay_buffer_resets_on_new_turn():
    # Two consecutive turns: buffer holds the latest turn only,
    # with seq restarting at 1.
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="one")]]),
        registry=ToolRegistry(),
    )
    await coord.run(game_id="g")
    assert len(coord.replay()) > 0

    # Second turn -- buffer resets.
    await coord.run(
        game_id="g",
        provider=ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="two")]]),
    )
    second = coord.replay()
    texts = [e["payload"].get("delta") for e in second if e["payload"].get("delta")]
    assert texts == ["two"]
    # Seq counter resets per turn.
    assert second[0]["payload"]["seq"] == 1


@pytest.mark.asyncio
async def test_clear_replay_empties_buffer():
    # Analysis stop drops the buffer so a fresh reconnect sees nothing,
    # not a stale snapshot of the last turn.
    bus = EventBus()
    await bus.subscribe()
    coord = AIAnalysisCoordinator(
        bus, ScriptedProvider(rounds=[[ProviderChunk(kind="text", text="x")]]),
        registry=ToolRegistry(),
    )
    await coord.run(game_id="g")
    assert len(coord.replay()) > 0
    coord.clear_replay()
    assert coord.replay() == []


@pytest.mark.asyncio
async def test_no_ai_tool_call_failed_event_on_success():
    async def ok(_input, *, cancel_token):
        return {"ok": True}

    reg = _make_registry({"piece_at": ok})
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="t1",
            tool_name="piece_at", tool_input={"square": "e4"},
        )],
        [ProviderChunk(kind="text", text="ok.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    coord = AIAnalysisCoordinator(bus, provider, registry=reg)

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    failed = [e for e in events if e.kind == "ai_tool_call_failed"]
    assert failed == []
