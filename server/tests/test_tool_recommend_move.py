"""`recommend_move` tool end-to-end (the dominance-check wiring).

`_better_for_stm` (the predicate) is unit-tested in test_recommend_margin.
These tests drive the whole tool: parse -> two real searches (free best +
candidate-restricted) -> accept / reject envelope. They pin the behaviors
the predicate alone can't show:

- a candidate that forces mate for the side to move is ACCEPTED even when
  the engine has a different, faster mate ("submit a different move" is
  wrong advice for a winning move),
- a candidate the engine beats by more than the margin is REJECTED with
  the recommendation_rejected envelope,
- the exact-move short-circuit accepts when the engine's free best IS the
  candidate.

A fixture-local fake emits `score mate` (the stock fakes only do cp) and
varies its reply by whether the `go` line carries `searchmoves` -- so the
free search and the candidate-restricted search can disagree.
"""
from __future__ import annotations

from pathlib import Path

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.engine_supervisor import EngineSupervisor
from sturddle_view.play.tools_engine import make_recommend_move_tool

from .conftest import _write_uci_stub


def _make_free_vs_restricted_fake(
    root: Path,
    name: str,
    *,
    free_info: str,
    restricted_info: str,
) -> str:
    """Fake UCI whose `go` reply depends on whether the command restricts
    the root via `searchmoves`. The candidate-restricted search (Search B)
    carries `searchmoves`; the free best (Search A) does not -- so the two
    can return different bestmoves/scores. `*_info` are full `info ...`
    line bodies (without the trailing newline) and must carry a pv so the
    tool can read bestmove = pv[0]."""
    # bestmove = each info line's first pv move (what the tool expects).
    free_bm = free_info.split(" pv ", 1)[1].split()[0]
    restricted_bm = restricted_info.split(" pv ", 1)[1].split()[0]
    extra = (
        "    elif line.startswith('go') or line == 'stop':\n"
        "        if 'searchmoves' in line:\n"
        f"            sys.stdout.write('{restricted_info}\\n')\n"
        f"            sys.stdout.write('bestmove {restricted_bm}\\n')\n"
        "        else:\n"
        f"            sys.stdout.write('{free_info}\\n')\n"
        f"            sys.stdout.write('bestmove {free_bm}\\n')\n"
        "        sys.stdout.flush()\n"
    )
    return _write_uci_stub(root, name, extra)


def _launcher_from_path(path: str, bus: EventBus):
    def _make() -> EngineSupervisor:
        return EngineSupervisor(engine_path=path, bus=bus)
    return _make


def _tool(engine_path: str, board: chess.Board):
    bus = EventBus()
    return make_recommend_move_tool(
        _launcher_from_path(engine_path, bus),
        bus=bus,
        board_provider=lambda: board,
    )


@pytest.mark.asyncio
async def test_winning_mate_accepted_even_when_engine_mates_faster(tmp_path: Path):
    # Candidate (g1f3, restricted) forces mate in 3 for white; the engine's
    # free best (a different move, e2e4) mates in 1. A faster mate must NOT
    # turn the candidate's forced win into a rejection.
    engine_path = _make_free_vs_restricted_fake(
        tmp_path, "rm_mate",
        free_info="info depth 20 score mate 1 nodes 100 time 10 pv e2e4",
        restricted_info="info depth 20 score mate 3 nodes 100 time 10 pv g1f3",
    )
    board = chess.Board()  # white to move
    tool = _tool(engine_path, board)
    out = await tool({"move": "Nf3", "depth": 20}, cancel_token=CancelToken())
    assert out.get("ok") is True, out
    assert out["uci"] == "g1f3"
    assert "post_move_fen" in out


@pytest.mark.asyncio
async def test_dominated_candidate_rejected(tmp_path: Path):
    # Candidate (g1f3) scores +10 cp; engine's free best (e2e4) scores
    # +300 cp -- beats by 290, well over the default 50cp margin. Reject.
    engine_path = _make_free_vs_restricted_fake(
        tmp_path, "rm_reject",
        free_info="info depth 20 score cp 300 nodes 100 time 10 pv e2e4",
        restricted_info="info depth 20 score cp 10 nodes 100 time 10 pv g1f3",
    )
    board = chess.Board()
    tool = _tool(engine_path, board)
    out = await tool({"move": "Nf3", "depth": 20}, cancel_token=CancelToken())
    assert out.get("error") == "recommendation_rejected", out
    assert "reason" in out
    assert out["engine_best_san"] == "e4"


@pytest.mark.asyncio
async def test_exact_match_accepts(tmp_path: Path):
    # Engine's free best IS the candidate (e2e4). Short-circuit accept --
    # never compare a move to itself.
    engine_path = _make_free_vs_restricted_fake(
        tmp_path, "rm_exact",
        free_info="info depth 20 score cp 30 nodes 100 time 10 pv e2e4",
        restricted_info="info depth 20 score cp 30 nodes 100 time 10 pv e2e4",
    )
    board = chess.Board()
    tool = _tool(engine_path, board)
    out = await tool({"move": "e4", "depth": 20}, cancel_token=CancelToken())
    assert out.get("ok") is True, out
    assert out["uci"] == "e2e4"


@pytest.mark.asyncio
async def test_within_margin_accepted(tmp_path: Path):
    # Candidate +20, engine best (different move) +60: beats by 40, under
    # the 50cp margin. Cosmetic preference, not a blunder -- accept.
    engine_path = _make_free_vs_restricted_fake(
        tmp_path, "rm_margin",
        free_info="info depth 20 score cp 60 nodes 100 time 10 pv e2e4",
        restricted_info="info depth 20 score cp 20 nodes 100 time 10 pv g1f3",
    )
    board = chess.Board()
    tool = _tool(engine_path, board)
    out = await tool({"move": "Nf3", "depth": 20}, cancel_token=CancelToken())
    assert out.get("ok") is True, out
    assert out["uci"] == "g1f3"


@pytest.mark.asyncio
async def test_shallow_request_floored_to_verification_depth(monkeypatch):
    # A model asking depth 5 must search at the verification floor, not 5 --
    # the dominance check can't confirm a move at a depth the model lowballed.
    from sturddle_view.play import tools_engine

    seen_depths = []

    async def fake_search(engine_launcher, board, limit, **kw):
        seen_depths.append(limit.depth)
        return {"depth": limit.depth, "score": None, "pv": []}, False

    monkeypatch.setattr(tools_engine, "_run_one_search", fake_search)
    board = chess.Board()
    tool = tools_engine.make_recommend_move_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board,
    )
    await tool({"move": "Nf3", "depth": 5}, cancel_token=CancelToken())
    # Both searches (free + candidate) floored to the default 25.
    assert seen_depths == [25, 25], seen_depths
