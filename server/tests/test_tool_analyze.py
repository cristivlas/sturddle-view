"""Slice C: `analyze` tool tests.

Happy path uses a real subprocess running a Python UCI fake
(`make_searching_fake_uci`) that answers `go` with a canned `info` +
`bestmove`. Real chess.engine plumbing; no mocks at the protocol
boundary. Error paths use lightweight stubs where a subprocess would
add no signal.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.tools_engine import make_analyze_tool
from sturddle_view.play.engine_supervisor import EngineSupervisor

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
    analyze = make_analyze_tool(_launcher_from_path(engine_path, bus))

    out = await analyze(
        {"fen": "startpos", "depth": 8},
        cancel_token=CancelToken(),
    )

    # Wire shape: numeric eval (cp or mate), depth, pv (SAN or UCI list),
    # best move convenience field, no error.
    assert "error" not in out, out
    assert out["score_cp"] == 42
    assert out["depth"] == 8
    assert isinstance(out["pv"], list)
    assert out["pv"][0] == "e2e4"
    assert out["bestmove"] == "e2e4"


@pytest.mark.asyncio
async def test_analyze_clamps_time_ms_above_hard_cap(tmp_path: Path, monkeypatch):
    # Set the cap low; pass time_ms above it; the tool should still run
    # (clamp, not reject) and the limit it constructs must be <= cap.
    from sturddle_view.play import tools_engine
    monkeypatch.setattr(tools_engine, "MAX_TIME_MS", 100)

    engine_path = make_searching_fake_uci(tmp_path, "clamp_t")
    bus = EventBus()
    captured_limit: dict = {}

    # Spy on chess.engine.Limit by reading what the supervisor saw.
    # Simpler approach: assert the tool's `limits_used` debug field
    # (deliberately exposed for tests; small surface, big signal).
    analyze = make_analyze_tool(_launcher_from_path(engine_path, bus))
    out = await analyze(
        {"fen": "startpos", "time_ms": 999_999},
        cancel_token=CancelToken(),
    )
    assert "error" not in out, out
    assert out["limits_used"]["time_ms"] == 100


@pytest.mark.asyncio
async def test_analyze_clamps_depth_above_hard_cap(tmp_path: Path, monkeypatch):
    from sturddle_view.play import tools_engine
    monkeypatch.setattr(tools_engine, "MAX_DEPTH", 4)

    engine_path = make_searching_fake_uci(tmp_path, "clamp_d", depth=2)
    bus = EventBus()
    analyze = make_analyze_tool(_launcher_from_path(engine_path, bus))
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

    analyze = make_analyze_tool(_no_launcher)
    out = await analyze({"fen": "not-a-fen"}, cancel_token=CancelToken())

    assert out.get("error") == "invalid_fen"
    assert "detail" in out


@pytest.mark.asyncio
async def test_analyze_missing_fen_with_structured_error():
    def _no_launcher() -> EngineSupervisor:
        raise AssertionError("engine should never be launched without a FEN")

    analyze = make_analyze_tool(_no_launcher)
    out = await analyze({}, cancel_token=CancelToken())

    assert out.get("error") == "missing_fen"


@pytest.mark.asyncio
async def test_analyze_cancel_returns_cancelled_marker(tmp_path: Path):
    # Pre-cancel the token, then call analyze. The fake engine returns
    # a single info chunk + bestmove on `go`, so the tool's loop body
    # runs at least once and observes cancelled=True before completion.
    # This is the deterministic cancel-path assertion -- it does NOT
    # exercise mid-search interruption against a long-running search
    # (which would require a non-timer-based sync mechanism we don't
    # yet have).
    engine_path = make_searching_fake_uci(
        tmp_path, "cancel_fake",
        score_cp=10, depth=3, bestmove="e2e4", pv="e2e4",
    )
    bus = EventBus()
    analyze = make_analyze_tool(_launcher_from_path(engine_path, bus))

    token = CancelToken()
    token.cancel()  # already cancelled before analyze runs

    out = await analyze({"fen": "startpos", "depth": 99}, cancel_token=token)

    assert out.get("cancelled") is True
    assert "score_cp" in out  # info chunk was captured before the stop
