"""End-of-turn recommend-verifier depth floor (`make_recommend_verifier`).

The verifier runs a searchmoves-restricted deep search on the recommended
move and floors the search depth at VERIFICATION_DEPTH -- so a shallow
(or omitted) model-supplied depth can't make the authoritative verdict a
shallow rubber-stamp. Going deeper than the floor is honored; the hard
cap (MAX_DEPTH) still wins over both.

The stock fakes emit a FIXED `info depth`, which can't prove the floor;
this module's depth-echoing fake parses the `go depth N` command and
echoes N back, so the returned payload's `depth` reflects exactly what
the verifier asked the engine to search.
"""
from __future__ import annotations

from pathlib import Path

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.engine_supervisor import EngineSupervisor
from sturddle_view.play import tools_engine
from sturddle_view.play.tools_engine import (
    MAX_DEPTH,
    VERIFICATION_DEPTH,
    make_recommend_verifier,
)

from .conftest import _write_uci_stub


def _make_depth_echo_fake(root: Path, name: str, *, score_cp: int = 20) -> str:
    """Fake UCI that echoes the requested `go depth N` back as the info
    line's depth -- so a test can assert which depth the caller searched.
    Defaults to depth 1 if no explicit depth token is present."""
    extra = (
        "    elif line.startswith('go') or line == 'stop':\n"
        "        toks = line.split()\n"
        "        d = 1\n"
        "        if 'depth' in toks:\n"
        "            try: d = int(toks[toks.index('depth') + 1])\n"
        "            except (ValueError, IndexError): d = 1\n"
        f"        sys.stdout.write(f'info depth {{d}} score cp {score_cp} nodes 1234 time 50 pv e2e4\\n')\n"
        "        sys.stdout.write('bestmove e2e4\\n')\n"
        "        sys.stdout.flush()\n"
    )
    return _write_uci_stub(root, name, extra)


def _launcher_from_path(path: str, bus: EventBus):
    def _make() -> EngineSupervisor:
        return EngineSupervisor(engine_path=path, bus=bus)
    return _make


def _verifier(tmp_path: Path, name: str):
    engine_path = _make_depth_echo_fake(tmp_path, name)
    bus = EventBus()
    board = chess.Board()
    verify = make_recommend_verifier(
        _launcher_from_path(engine_path, bus),
        bus=bus,
        board_provider=lambda: board,
    )
    return verify, board


@pytest.mark.asyncio
async def test_floor_applies_when_depth_omitted(tmp_path: Path):
    # depth=None -> the search is floored to VERIFICATION_DEPTH.
    verify, board = _verifier(tmp_path, "rv_none")
    payload = await verify(chess.Move.from_uci("e2e4"), None, CancelToken())
    assert payload is not None
    assert payload["depth"] == VERIFICATION_DEPTH


@pytest.mark.asyncio
async def test_floor_applies_when_depth_below_floor(tmp_path: Path):
    # A shallow model-supplied depth is raised to the floor, never used as-is.
    verify, board = _verifier(tmp_path, "rv_shallow")
    payload = await verify(chess.Move.from_uci("e2e4"), 2, CancelToken())
    assert payload is not None
    assert payload["depth"] == VERIFICATION_DEPTH


@pytest.mark.asyncio
async def test_deeper_request_above_floor_is_honored(tmp_path: Path, monkeypatch):
    # When the model asks deeper than the floor (but under the cap), honor
    # it. Defaults ship MAX_DEPTH == VERIFICATION_DEPTH (no headroom), so
    # lift the cap for this case -- verify() reads MAX_DEPTH at call time.
    deeper = VERIFICATION_DEPTH + 3
    monkeypatch.setattr(tools_engine, "MAX_DEPTH", deeper + 5)
    verify, board = _verifier(tmp_path, "rv_deeper")
    payload = await verify(chess.Move.from_uci("e2e4"), deeper, CancelToken())
    assert payload is not None
    assert payload["depth"] == deeper


@pytest.mark.asyncio
async def test_request_above_cap_clamps_to_max_depth(tmp_path: Path):
    # The hard cap wins over a too-deep request (determinism guardrail).
    verify, board = _verifier(tmp_path, "rv_capped")
    payload = await verify(chess.Move.from_uci("e2e4"), MAX_DEPTH + 50, CancelToken())
    assert payload is not None
    assert payload["depth"] == MAX_DEPTH


@pytest.mark.asyncio
async def test_illegal_move_returns_none(tmp_path: Path):
    # Move not legal in the live position -> no verification payload.
    verify, board = _verifier(tmp_path, "rv_illegal")
    # e7e5 is black's move; white to move at the start, so it's illegal here.
    payload = await verify(chess.Move.from_uci("e7e5"), None, CancelToken())
    assert payload is None
