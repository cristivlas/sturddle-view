"""Position-check wiring in the AIAnalysisCoordinator loop.

Every round's prose is checked against the live board. A mismatch emits an
ai_position_note event (UI self-correction) and injects a fact-anchored
[position check] user message so the model corrects itself next round. A
re-flagged item is struck again but draws no second corrective.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EVT_AI_POSITION_NOTE, EventBus
from sturddle_view.llm import ProviderChunk, ScriptedProvider, ToolRegistry, ToolSpec
from sturddle_view.play.ai_analysis import (
    AIAnalysisCoordinator,
    _PositionCheck,
    _POSITION_CHECK_PREFIX,
)


# These tests lock pure regex-loop behavior (note emission, corrective
# injection, round counts). The semantic judge is a separate layer with its
# own suite (test_position_judge); disable it here so its extra stream() call
# doesn't perturb the scripted-provider round budget or the assertions.
@pytest.fixture(autouse=True)
def _no_semantic_check(monkeypatch):
    monkeypatch.setattr(
        "sturddle_view.play.ai_analysis.SEMANTIC_CHECK_ENABLED", False,
    )


# Black to move; g6 and c1 are empty (false-claim targets).
_FEN = "r2qr1k1/5ppp/p4n2/1pbP1bB1/8/2Nn1B2/PP1Q1PPP/1N1R1RK1 b - - 3 17"


async def _drain_until_done(queue) -> list:
    events = []
    while True:
        evt = await queue.get()
        events.append(evt)
        if evt.payload.get("done"):
            return events


def _coord(provider, board):
    bus = EventBus()
    coord = AIAnalysisCoordinator(
        bus, provider, registry=ToolRegistry(), board_provider=lambda: board,
    )
    return coord, bus


def _last_user_texts(provider: ScriptedProvider) -> list[str]:
    """User-role message contents from the most recent round's input."""
    msgs = provider.last_call["messages"] if provider.last_call else []
    out = []
    for m in msgs:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            out.append(m["content"])
    return out


@pytest.mark.asyncio
async def test_clean_prose_emits_no_note_and_no_injection():
    board = chess.Board(_FEN)
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="Black is slightly better here."),
    ]])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert not any(e.kind == EVT_AI_POSITION_NOTE for e in events)
    assert provider.stream_calls == 1  # natural exit, no extra round


@pytest.mark.asyncio
async def test_false_claim_emits_note_and_injects_corrective():
    board = chess.Board(_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="The bishop on h6 dominates.")],
        [ProviderChunk(kind="text", text="Corrected: the bishop is on f5.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    # The note carries the exact prose span (with article) for the client to
    # strike -- prose was "The bishop on h6 dominates."
    assert notes[0].payload["surfaces"] == ["The bishop on h6"]
    # The corrective is injected, forcing a second round.
    assert provider.stream_calls == 2
    injected = _last_user_texts(provider)
    assert any(t.startswith(_POSITION_CHECK_PREFIX) for t in injected)
    # Fact-anchored: the corrective states the square is empty.
    assert any("h6 is empty" in t for t in injected)


@pytest.mark.asyncio
async def test_false_pin_claim_emits_note_and_injects_tactics_fact():
    # Bb5 pins the c6 knight; the f3 knight is not pinned.
    board = chess.Board("r1bqkbnr/ppp2ppp/2np4/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4")
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="The knight on f3 is pinned, so Nd4 fails.")],
        [ProviderChunk(kind="text", text="Corrected: the c6 knight is the pinned one.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    # The whole clause is struck.
    assert notes[0].payload["surfaces"] == ["The knight on f3 is pinned"]
    assert provider.stream_calls == 2
    injected = _last_user_texts(provider)
    assert any(t.startswith(_POSITION_CHECK_PREFIX) for t in injected)
    assert any(
        "black knight on c6 pinned to black king on e8 by white bishop on b5" in t
        for t in injected
    )


# White to move; both bishops (d3, e6) are light-squared, so a "dark-squared
# bishop" reference matches nothing -- the reported hallucination.
_BISHOP_FEN = "2n1rk2/p1R2p2/2NRb1p1/1P5p/4P2P/3B1PP1/5K2/2r5 w - - 3 41"


def test_board_labels_exclude_bishop_flags():
    # Bishop-color flags are precomputed board facts the judge has repeatedly
    # cleared wrongly; the regex verdict is final for this class, so no
    # bishop label ever reaches the judge.
    pc = _PositionCheck(
        chess.Board(), [], [], [],
        fact_triples=[
            ("the light-squared bishop", "light-squared bishop on d6",
             "d6 is dark-squared"),
            ("the dark-squared bishop", "dark-squared bishop",
             "no dark-squared bishop on the board"),
        ],
    )
    assert pc.board_labels == []


@pytest.mark.asyncio
async def test_false_bishop_color_emits_note_and_injects_corrective():
    board = chess.Board(_BISHOP_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="Rd4 hits the dark-squared bishop.")],
        [ProviderChunk(kind="text", text="Corrected: the bishop on e6 is light-squared.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    # Surface includes the article (like piece claims) so the client strikes
    # "the dark-squared bishop" whole, not a dangling "the".
    assert notes[0].payload["surfaces"] == ["the dark-squared bishop"]
    assert provider.stream_calls == 2
    injected = _last_user_texts(provider)
    assert any(t.startswith(_POSITION_CHECK_PREFIX) for t in injected)
    # Fact-anchored: bare ref (no side named) lists the real bishops, both
    # light-squared, so the model sees there is no dark-squared bishop at all.
    assert any(
        "no dark-squared bishop on the board" in t
        and "d3 is light-squared" in t and "e6 is light-squared" in t
        for t in injected
    )


def test_tool_mention_after_future_line_still_caught():
    # A future move number truncates the board view, but a tool mention past it
    # is a style violation everywhere -- scanned on the full prose.
    board = chess.Board()  # move 1, so "2.Nf3" is a future line
    coord, _bus = _coord(None, board)
    pc = coord._position_check([
        ProviderChunk(kind="text", text="Then 2.Nf3 develops. The tool agrees."),
    ])
    assert pc.tool_mentions == ["the tool"]
    assert pc.hit


def test_tool_mention_produces_corrective_clause():
    # A caught tool reference asks the model to remove it and rewrite -- no
    # strike, no "isn't legal" wording.
    pc = _PositionCheck(
        chess.Board(_FEN),
        move_pairs=[],
        claim_triples=[],
        line_pairs=[],
        tool_mentions=["the tool"],
    )
    msg = AIAnalysisCoordinator._position_check_message(pc)
    assert '"the tool"' in msg
    assert "never name the tools or engine" in msg
    assert "isn't legal" not in msg


def test_move_named_as_line_and_token_appears_once_in_corrective():
    # A move flagged both as a broken line and standalone in prose must not
    # repeat its fact in the corrective sent to the model.
    pc = _PositionCheck(
        chess.Board(_FEN),
        move_pairs=[("Bb5", "Bb5")],
        claim_triples=[],
        line_pairs=[("Bb5", "Bb5")],
    )
    msg = AIAnalysisCoordinator._position_check_message(pc)
    assert msg.count("Bb5 isn't legal") == 1


def test_repeat_keys_do_not_collide_across_item_types():
    # A claim keyed by square e4 must not mark the pawn move e4 as a repeat.
    claim = _PositionCheck(
        chess.Board(_FEN), [], [("the knight on e4", "knight on e4", "e4")], [],
    )
    move = _PositionCheck(chess.Board(_FEN), [("e4", "e4")], [], [])
    new, repeat = move.partition(claim.keys)
    assert new.move_pairs == [("e4", "e4")]
    assert not repeat.hit


@pytest.mark.asyncio
async def test_repeat_of_reworded_claim_is_struck_and_shipped():
    # Round 2's acknowledgment rewords the round-1 claim; the repeat key is
    # the square, so it's struck with no second corrective and the turn ends
    # after round 2 instead of looping to the round cap.
    board = chess.Board(_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="White bishop on c6 is strong.")],
        [ProviderChunk(kind="text", text="Actually, bishop on c6 isn't right.")],
        [ProviderChunk(kind="text", text="Never reached.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert [n.payload["surfaces"] for n in notes] == [
        ["White bishop on c6"], ["bishop on c6"],
    ]
    assert provider.stream_calls == 2
    assert not events[-1].payload.get("round_cap")


@pytest.mark.asyncio
async def test_prose_move_to_unreachable_square_flagged():
    # "bishop to a1" is an impossible move; the note carries the exact prose
    # span (including the verb) for the client to strike.
    board = chess.Board(_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="The bishop goes to a1 winning.")],
        [ProviderChunk(kind="text", text="Corrected.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    assert notes[0].payload["surfaces"] == ["The bishop goes to a1"]
    assert provider.stream_calls == 2


@pytest.mark.asyncio
async def test_no_board_provider_is_a_noop():
    provider = ScriptedProvider(rounds=[[
        ProviderChunk(kind="text", text="The bishop on g6 dominates."),
    ]])
    bus = EventBus()
    coord = AIAnalysisCoordinator(bus, provider, registry=ToolRegistry())
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    assert not any(e.kind == EVT_AI_POSITION_NOTE for e in events)
    assert provider.stream_calls == 1


# Black to move; the white queen is on b2 -- the reported "white queen on the
# c-file" hallucination (see test_position_check._WILD_C_FILE_FEN).
_C_FILE_FEN = "r2q1rk1/pp1bpp1p/2np1np1/1B6/P3P2P/5N2/1Q1N1PP1/R3R1K1 b - - 5 13"


@pytest.mark.asyncio
async def test_false_file_claim_emits_note_and_injects_corrective():
    board = chess.Board(_C_FILE_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="Rc8 faces the white queen on the c-file.")],
        [ProviderChunk(kind="text", text="Corrected: the white queen is on b2.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    assert notes[0].payload["surfaces"] == ["white queen on the c-file"]
    injected = _last_user_texts(provider)
    assert any(
        t.startswith(_POSITION_CHECK_PREFIX)
        and "no white queen on the c-file; the white queen is on b2" in t
        for t in injected
    )


@pytest.mark.asyncio
async def test_repeat_of_file_claim_is_struck_and_shipped():
    # The file label joins the repeat keys: re-asserting "white queen on the
    # c-file" in round 2 is struck and ends the turn, no third round.
    board = chess.Board(_C_FILE_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="Rc8 faces the white queen on the c-file.")],
        [ProviderChunk(kind="text", text="The white queen on the c-file is loose.")],
        [ProviderChunk(kind="text", text="Never reached.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 2
    assert provider.stream_calls == 2


@pytest.mark.asyncio
async def test_false_file_openness_emits_note_and_injects_corrective():
    # Bare "a semi-open file" bound to 13...Rc8's c-file, which has no pawns.
    board = chess.Board(_C_FILE_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="13...Rc8 places the rook on a semi-open file.")],
        [ProviderChunk(kind="text", text="Corrected: the c-file has no pawns.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert len(notes) == 1
    assert notes[0].payload["surfaces"] == ["a semi-open file"]
    injected = _last_user_texts(provider)
    assert any(
        t.startswith(_POSITION_CHECK_PREFIX)
        and "the c-file is open: no pawns on it" in t
        for t in injected
    )


# Black to move; Re6 is legal and e6 is empty (the live repro).
_RE6_FEN = "1rb1r1k1/ppp2pp1/2n2q1p/4p3/2Pp3N/3P2PP/PP1QPPB1/2R1K2R b K - 1 14"


@pytest.mark.asyncio
async def test_false_illegality_claim_emits_note_and_injects_legal_fact():
    board = chess.Board(_RE6_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="Re6 is illegal, so the rook stays.")],
        [ProviderChunk(kind="text", text="Corrected.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert [n.payload["surfaces"] for n in notes] == [["Re6 is illegal"]]
    injected = _last_user_texts(provider)
    assert any(
        t.startswith(_POSITION_CHECK_PREFIX) and "Re6 is legal here" in t
        for t in injected
    )


@pytest.mark.asyncio
async def test_false_occupancy_claim_emits_note_and_injects_square_fact():
    board = chess.Board(_RE6_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="The rook cannot land because e6 is occupied.")],
        [ProviderChunk(kind="text", text="Corrected.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert [n.payload["surfaces"] for n in notes] == [["e6 is occupied"]]
    injected = _last_user_texts(provider)
    assert any("e6 is empty" in t for t in injected)


@pytest.mark.asyncio
async def test_tool_label_leak_corrected_once_then_struck_and_shipped():
    # The live leak: a "Verdict:" label copied from the delegate result. The
    # first draws a corrective; the repeat is struck and the turn ends.
    board = chess.Board(_FEN)
    provider = ScriptedProvider(rounds=[
        [ProviderChunk(kind="text", text="Verdict: holds, the knight on f6 guards d5.")],
        [ProviderChunk(kind="text", text="Verdict: the knight on f6 guards d5.")],
        [ProviderChunk(kind="text", text="Never reached.")],
    ])
    coord, bus = _coord(provider, board)
    queue = await bus.subscribe()

    await coord.run(game_id="g")
    events = await _drain_until_done(queue)

    notes = [e for e in events if e.kind == EVT_AI_POSITION_NOTE]
    assert [n.payload["surfaces"] for n in notes] == [["Verdict:"], ["Verdict:"]]
    assert provider.stream_calls == 2
    injected = _last_user_texts(provider)
    assert any(
        t.startswith(_POSITION_CHECK_PREFIX) and '"Verdict:"' in t for t in injected
    )


async def _noop_tool(_input, *, cancel_token):
    return {"ok": True}


def test_registered_tool_names_are_leak_names():
    board = chess.Board(_FEN)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="recommend_move", description="d", input_schema={"type": "object"}),
        _noop_tool,
    )
    coord = AIAnalysisCoordinator(
        EventBus(), None, registry=registry, board_provider=lambda: board,
    )
    pc = coord._position_check([
        ProviderChunk(kind="text", text="Recommend Move: Nxd5 wins a pawn."),
    ])
    assert pc.tool_leaks == [("Recommend Move:", "recommend_move")]
    assert pc.surfaces == ["Recommend Move:"]
