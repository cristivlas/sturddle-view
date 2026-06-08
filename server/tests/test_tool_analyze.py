"""`analyze` tool tests.

Happy path uses a real subprocess running a Python UCI fake
(`make_searching_fake_uci`) that answers `go` with a canned `info` +
`bestmove`. Real chess.engine plumbing; no mocks at the protocol
boundary. Error paths use lightweight stubs where a subprocess would
add no signal.

Direct unit tests on `_score_to_cp` at the bottom pin the LLM-facing
wire shape (cp / pawns / score_text / mate) without spawning anything.
"""
from __future__ import annotations

from pathlib import Path

import chess
from chess.engine import Cp, Mate, PovScore
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.engine_supervisor import EngineSupervisor
from sturddle_view.play.tools_engine import _score_to_cp, make_analyze_tool

from .conftest import make_searching_fake_uci


def _launcher_from_path(path: str, bus: EventBus):
    """Returns a callable suitable as `engine_launcher` for
    make_analyze_tool -- one call yields a fresh supervisor pinned to
    the fake engine path."""
    def _make() -> EngineSupervisor:
        return EngineSupervisor(engine_path=path, bus=bus)
    return _make


@pytest.mark.asyncio
async def test_analyze_returns_eval_pv_depth_for_known_position(tmp_path: Path):
    engine_path = make_searching_fake_uci(
        tmp_path, "ana_fake",
        score_cp=42, depth=8, bestmove="e2e4", pv="e2e4 e7e5",
    )
    bus = EventBus()
    analyze = make_analyze_tool(_launcher_from_path(engine_path, bus), bus=bus)

    out = await analyze(
        {"fen": "startpos", "depth": 8},
        cancel_token=CancelToken(),
    )

    # Wire shape: numeric eval (cp or mate), depth, pv (SAN or UCI list),
    # best move convenience field, no error.
    assert "error" not in out, out
    assert out["score_cp"] == 42
    # Pawn-units + presentation string accompany score_cp so the LLM
    # has zero room to misinterpret centipawns as pawns.
    assert out["score_pawns"] == 0.42
    assert out["score_text"] == "+0.42"
    assert out["depth"] == 8
    assert isinstance(out["pv"], list)
    assert out["pv"][0] == "e2e4"
    assert out["bestmove"] == "e2e4"


@pytest.mark.asyncio
async def test_analyze_clamps_depth_above_hard_cap(tmp_path: Path, monkeypatch):
    from sturddle_view.play import tools_engine
    monkeypatch.setattr(tools_engine, "MAX_DEPTH", 4)

    engine_path = make_searching_fake_uci(tmp_path, "clamp_d", depth=2)
    bus = EventBus()
    analyze = make_analyze_tool(_launcher_from_path(engine_path, bus), bus=bus)
    out = await analyze(
        {"fen": "startpos", "depth": 999},
        cancel_token=CancelToken(),
    )
    assert "error" not in out, out
    assert out["limits_used"]["depth"] == 4


@pytest.mark.asyncio
async def test_analyze_rejects_invalid_fen_with_structured_error():
    # No subprocess needed -- FEN parsing fails before any spawn.
    def _no_launcher() -> EngineSupervisor:
        raise AssertionError("engine should never be launched for bad FEN")

    analyze = make_analyze_tool(_no_launcher, bus=EventBus())
    out = await analyze({"fen": "not-a-fen"}, cancel_token=CancelToken())

    assert out.get("error") == "invalid_fen"
    assert "detail" in out


@pytest.mark.asyncio
async def test_analyze_missing_fen_with_structured_error():
    def _no_launcher() -> EngineSupervisor:
        raise AssertionError("engine should never be launched without a FEN")

    analyze = make_analyze_tool(_no_launcher, bus=EventBus())
    out = await analyze({}, cancel_token=CancelToken())

    assert out.get("error") == "missing_fen"


@pytest.mark.asyncio
async def test_analyze_cancel_returns_cancelled_marker(tmp_path: Path):
    # Pre-cancelled token: pump bails out before the first iteration.
    # No score captured; just the cancelled marker. Mid-search cancel
    # (after first info) is covered by test_engine_info_pump.
    engine_path = make_searching_fake_uci(
        tmp_path, "cancel_fake",
        score_cp=10, depth=3, bestmove="e2e4", pv="e2e4",
    )
    bus = EventBus()
    analyze = make_analyze_tool(_launcher_from_path(engine_path, bus), bus=bus)

    token = CancelToken()
    token.cancel()

    out = await analyze({"fen": "startpos", "depth": 99}, cancel_token=token)

    assert out.get("cancelled") is True


@pytest.mark.asyncio
async def test_analyze_publishes_engine_info_to_bus(tmp_path: Path):
    """The PV table + board arrow want engine_info events. Without this,
    AI mode shows an empty PV panel even though the engine is running.
    Pre-refactor this was a separate code path entirely; now both paths
    flow through pump_engine_info -- the test pins that the tool wires
    the bus through correctly."""
    engine_path = make_searching_fake_uci(
        tmp_path, "pub_fake",
        score_cp=25, depth=5, bestmove="d2d4", pv="d2d4 d7d5",
    )
    bus = EventBus()
    queue = await bus.subscribe()
    analyze = make_analyze_tool(_launcher_from_path(engine_path, bus), bus=bus)

    out = await analyze({"fen": "startpos", "depth": 5}, cancel_token=CancelToken())
    assert "error" not in out, out

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    # First marker: engine_search_start clears the PV panel.
    kinds = [e.kind for e in events]
    assert "engine_search_start" in kinds
    # At least one engine_info with the pv shows up so the PV table fills
    # and the board arrow draws.
    infos = [e for e in events if e.kind == "engine_info"]
    assert infos, "tool did not publish any engine_info events"
    last = infos[-1].payload
    assert last.get("pv_uci") == ["d2d4", "d7d5"]
    assert last.get("score", {}).get("cp") == 25


@pytest.mark.asyncio
async def test_analyze_passes_game_id_to_published_events(tmp_path: Path):
    """game_id_provider lets engine_info events carry the live game's id
    so the WS muxing routes them to the right session."""
    engine_path = make_searching_fake_uci(
        tmp_path, "gid_fake", score_cp=0, depth=2, bestmove="e2e4", pv="e2e4",
    )
    bus = EventBus()
    queue = await bus.subscribe()
    analyze = make_analyze_tool(
        _launcher_from_path(engine_path, bus), bus=bus,
        game_id_provider=lambda: "g-live",
    )

    await analyze({"fen": "startpos", "depth": 2}, cancel_token=CancelToken())

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    # Only engine_info + engine_search_start are tool-published. Other
    # events (uci_log, etc.) come from the EngineSupervisor transport
    # and legitimately have no game_id.
    tool_events = [e for e in events if e.kind in ("engine_info", "engine_search_start")]
    assert tool_events, "tool did not publish any events"
    for e in tool_events:
        assert e.game_id == "g-live", f"unexpected game_id: {e.game_id}"


# ---------- _score_to_cp wire-shape unit tests --------------------------
# Direct tests so each presentation-format branch is pinned without
# spawning an engine. The wire shape is the LLM's only source of truth
# for evaluations -- silent regressions here cause the 100x cp/pawn
# misread we shipped fixes for.


def test_score_to_cp_white_advantage():
    out = _score_to_cp(PovScore(Cp(45), chess.WHITE))
    assert out["score_cp"] == 45
    assert out["score_pawns"] == 0.45
    assert out["score_text"] == "+0.45"
    assert "mate" not in out


def test_score_to_cp_black_advantage():
    # Side-to-move = black, score from black's POV is +150 (black ahead).
    # White POV must surface as -150 / -1.5.
    out = _score_to_cp(PovScore(Cp(150), chess.BLACK))
    assert out["score_cp"] == -150
    assert out["score_pawns"] == -1.5
    assert out["score_text"] == "-1.50"


def test_score_to_cp_mate_for_white():
    # 5 plies = mate in 3 (plies/2 rounded up).
    out = _score_to_cp(PovScore(Mate(5), chess.WHITE))
    assert out["mate"] == 5
    assert out["score_text"] == "+M3"


def test_score_to_cp_mate_against_white():
    # Black mates white in 2 (4 plies, side-to-move is white).
    out = _score_to_cp(PovScore(Mate(-4), chess.WHITE))
    assert out["mate"] == -4
    assert out["score_text"] == "-M2"


def test_score_to_cp_none_returns_empty():
    assert _score_to_cp(None) == {}
