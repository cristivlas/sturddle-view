"""End-to-end dedup cache behavior in AIAnalysisCoordinator.

Two identical tool calls back-to-back must:
- dispatch the tool exactly once,
- emit one ai_tool_call event (not two) so the UI shows one dot,
- still feed both rounds' messages with the (same) tool_result.

Uses ScriptedProvider so the agent loop runs deterministically without
any LLM or engine.
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
from sturddle_view.play.ai_analysis import AIAnalysisCoordinator


def _make_registry(handlers: dict) -> ToolRegistry:
    reg = ToolRegistry()
    for name, fn in handlers.items():
        reg.register(
            ToolSpec(name=name, description=name, input_schema={"type": "object"}),
            fn,
        )
    return reg


async def _drain_until_done(queue: asyncio.Queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


@pytest.mark.asyncio
async def test_identical_top_moves_calls_dispatched_once():
    """Two back-to-back identical top_moves calls => one dispatch."""
    dispatch_count = 0

    async def fake_top_moves(payload, *, cancel_token):
        nonlocal dispatch_count
        dispatch_count += 1
        return {
            "side_to_move": "white",
            "candidates": [
                {"move_uci": "g1f3", "move_san": "Nf3", "score_cp": 30},
            ],
        }

    reg = _make_registry({"top_moves": fake_top_moves})

    # Round 0: tool_use. Round 1: identical tool_use. Round 2: terminal text.
    same_input = {"moves": ["Nf3", "Nc3"], "depth": 12}
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use",
            tool_use_id="tu_1",
            tool_name="top_moves",
            tool_input=same_input,
        )],
        [ProviderChunk(
            kind="tool_use",
            tool_use_id="tu_2",
            tool_name="top_moves",
            tool_input=dict(same_input),  # new dict, same values
        )],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    board = chess.Board()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=lambda: board,
    )

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    # Dispatch fired exactly once.
    assert dispatch_count == 1, f"expected 1 dispatch, got {dispatch_count}"

    # Bus saw exactly one ai_tool_call event (the second is suppressed).
    tool_calls = [e for e in events if e.kind == "ai_tool_call"]
    assert len(tool_calls) == 1, [e.payload for e in tool_calls]
    assert tool_calls[0].payload["name"] == "top_moves"
    # complete fires once per real dispatch (paired with the ai_tool_call
    # dot); cache hits are silent so the client sees one dot + one complete.
    completes = [e for e in events if e.kind == "ai_tool_call_complete"]
    assert len(completes) == 1
    assert completes[0].payload["tool_use_id"] == "tu_1"


@pytest.mark.asyncio
async def test_different_top_moves_calls_both_dispatched():
    """Two top_moves calls with different inputs both dispatch."""
    dispatch_count = 0

    async def fake_top_moves(payload, *, cancel_token):
        nonlocal dispatch_count
        dispatch_count += 1
        return {"side_to_move": "white", "candidates": []}

    reg = _make_registry({"top_moves": fake_top_moves})

    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_1", tool_name="top_moves",
            tool_input={"moves": ["Nf3"], "depth": 12},
        )],
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_2", tool_name="top_moves",
            tool_input={"moves": ["Nc3"], "depth": 12},  # different move
        )],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    board = chess.Board()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=lambda: board,
    )

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert dispatch_count == 2
    tool_calls = [e for e in events if e.kind == "ai_tool_call"]
    assert len(tool_calls) == 2


@pytest.mark.asyncio
async def test_dedup_with_exact_live_inputs():
    """Reproduce the live observation: identical top_moves call with
    the same moves list, on a real mid-game board, must hit dedup."""
    from sturddle_view.play.ai_analysis import _NORMALIZERS

    # Approximate the live FEN: white to move, some position where
    # gxh4, Qd2, Ng2, Ne2 all parse (or some don't and trip the None).
    # Use a board where these all parse to be sure.
    fen = "5rk1/2qbbppk/rpn1p2n/2p4p/P1PpP1Pp/3P1NPP/3QN1BK/R4R2 w - - 0 19"
    board = chess.Board(fen)
    payload = {"moves": ["gxh4", "Qd2", "Ng2", "Ne2"]}
    k1 = _NORMALIZERS["top_moves"](payload, board)
    k2 = _NORMALIZERS["top_moves"](dict(payload), board)
    # Key must be deterministic; same inputs -> same key.
    assert k1 == k2, f"{k1!r} != {k2!r}"


@pytest.mark.asyncio
async def test_dedup_material_keys_on_canonical_fen():
    """material dedups on the canonicalized FEN: surrounding whitespace
    AND the move counters wash out, so the same board with different
    clocks shares a key; missing or unparseable FENs return None (miss)."""
    from sturddle_view.play.ai_analysis import _NORMALIZERS

    norm = _NORMALIZERS["material"]
    fen = "r1bqk2r/pppp1ppp/2n5/8/1b6/2N2N2/PPPP1PPP/R1BQ1RK1 w kq - 0 1"
    k1 = norm({"fen": fen}, None)
    # Padded whitespace + different halfmove/fullmove counters: same board.
    k2 = norm({"fen": "  " + fen.replace("0 1", "9 42") + "  "}, None)
    assert k1 is not None and k1 == k2, f"{k1!r} != {k2!r}"
    # A different position must not collide.
    other = norm({"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"}, None)
    assert other != k1
    # Missing / unparseable FEN -> None so the dedup cache skips it.
    assert norm({"fen": "not-a-fen"}, None) is None
    assert norm({}, None) is None


@pytest.mark.asyncio
async def test_dedup_across_prose_separated_rounds():
    """An identical tool_use in a later round dedup-hits even when an
    intervening round produced only prose. Real-world: the model emitted
    the same top_moves call across two rounds -- we want one dispatch and
    one panel dot, not two."""
    dispatch_count = 0

    async def fake_top_moves(payload, *, cancel_token):
        nonlocal dispatch_count
        dispatch_count += 1
        return {"side_to_move": "white", "candidates": []}

    reg = _make_registry({"top_moves": fake_top_moves})

    same_input = {"moves": ["Nf3"], "depth": 12}
    # Round 0: prose + tool_use. Round 1: identical tool_use -- should
    # dedup-hit. Round 2: terminal text.
    provider = ScriptedProvider(rounds=[
        [
            ProviderChunk(kind="text", text="Consider the knight, then probe."),
            ProviderChunk(
                kind="tool_use", tool_use_id="tu_1", tool_name="top_moves",
                tool_input=same_input,
            ),
        ],
        [
            ProviderChunk(
                kind="tool_use", tool_use_id="tu_2", tool_name="top_moves",
                tool_input=dict(same_input),
            ),
        ],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    board = chess.Board()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=lambda: board,
    )

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert dispatch_count == 1, f"expected 1 dispatch, got {dispatch_count}"
    tool_calls = [e for e in events if e.kind == "ai_tool_call"]
    assert len(tool_calls) == 1


@pytest.mark.asyncio
async def test_intervening_different_tool_clears_slot():
    """Sequence A, B, A: B should NOT be a dedup hit for A; the second
    A also misses because the slot now holds B."""
    counts = {"a": 0, "b": 0}

    async def fake_a(payload, *, cancel_token):
        counts["a"] += 1
        return {"side_to_move": "white", "candidates": []}

    async def fake_b(payload, *, cancel_token):
        counts["b"] += 1
        return {"legal": True, "uci": "g1f3", "san": "Nf3"}

    reg = _make_registry({"top_moves": fake_a, "validate_move": fake_b})

    a_input = {"moves": ["Nf3"], "depth": 12}
    b_input = {"move": "Nf3"}
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_1", tool_name="top_moves",
            tool_input=a_input,
        )],
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_2", tool_name="validate_move",
            tool_input=b_input,
        )],
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_3", tool_name="top_moves",
            tool_input=dict(a_input),
        )],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    board = chess.Board()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=lambda: board,
    )

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    # Each of the three distinct slots dispatched.
    assert counts == {"a": 2, "b": 1}
    tool_calls = [e for e in events if e.kind == "ai_tool_call"]
    assert len(tool_calls) == 3


@pytest.mark.asyncio
async def test_uncacheable_call_does_not_clear_slot():
    """Regression from live observation: A (cacheable), B (key=None),
    A (cacheable, identical) -- the second A must hit the dedup cache,
    because B was uncacheable but not invalidating. We saw the slot
    cleared in production when an intervening validate_move call had
    a move that didn't parse on the live board (key=None)."""
    counts = {"a": 0, "b": 0}

    async def fake_a(payload, *, cancel_token):
        counts["a"] += 1
        return {"side_to_move": "white", "candidates": []}

    async def fake_b(payload, *, cancel_token):
        counts["b"] += 1
        return {"legal": False, "error": None}

    reg = _make_registry({"top_moves": fake_a, "validate_move": fake_b})

    a_input = {"moves": ["Nf3"], "depth": 12}
    # validate_move with a move that won't parse on startpos -> key=None.
    b_input = {"move": "Qxh7#"}
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_1", tool_name="top_moves",
            tool_input=a_input,
        )],
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_2", tool_name="validate_move",
            tool_input=b_input,
        )],
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_3", tool_name="top_moves",
            tool_input=dict(a_input),
        )],
        [ProviderChunk(kind="text", text="done.")],
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    board = chess.Board()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=lambda: board,
    )

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    # First A dispatches; B dispatches (uncacheable but real); second A
    # is a dedup hit -> total a=1, b=1.
    assert counts == {"a": 1, "b": 1}
    tool_calls = [e for e in events if e.kind == "ai_tool_call"]
    # Two UI dots: first A, then B. The second A is suppressed.
    assert len(tool_calls) == 2


@pytest.mark.asyncio
async def test_identical_error_call_dedups():
    """Identical calls that both produce an error result should still
    dedup -- second dispatch is a slot hit and produces no second UI dot.
    Regression: recommend_move rejected the same move twice and emitted
    two full dots/cards instead of one."""
    dispatch_count = 0

    async def fake_recommend(payload, *, cancel_token):
        nonlocal dispatch_count
        dispatch_count += 1
        return {
            "error": "recommendation_rejected",
            "uci": "e2e4", "san": "e4",
            "engine_best_san": "Nf3",
            "reason": "Engine prefers Nf3. Submit a different move.",
        }

    reg = _make_registry({"recommend_move": fake_recommend})

    same = {"move": "e4"}
    # Both attempts reject (dedup -> one dispatch). The narrator then keeps
    # being nudged toward an accepted move; here it stalls with prose, so
    # the nudge fires once and the stall guard ends the turn. The trailing
    # rounds cover that tail without changing the dedup point.
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_1", tool_name="recommend_move",
            tool_input=same,
        )],
        [ProviderChunk(
            kind="tool_use", tool_use_id="tu_2", tool_name="recommend_move",
            tool_input=dict(same),
        )],
        [ProviderChunk(kind="text", text="done.")],       # clean exit -> nudge #1
        [ProviderChunk(kind="text", text="still done.")],  # stall -> guard stops
    ])
    bus = EventBus()
    queue = await bus.subscribe()
    board = chess.Board()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=reg, board_provider=lambda: board,
    )

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert dispatch_count == 1, f"expected 1 dispatch, got {dispatch_count}"
    tool_calls = [e for e in events if e.kind == "ai_tool_call"]
    assert len(tool_calls) == 1
