"""`recommend_move` tool end-to-end (the dominance-check wiring).

`_better_for_stm` (the predicate) is unit-tested in test_recommend_margin.
These tests drive the whole tool: parse -> two real searches (free best +
candidate-restricted) -> accept / reject envelope. They pin the behaviors
the predicate alone can't show:

- a candidate that forces mate for the side to move is ACCEPTED even when
  the engine has a different, faster mate ("submit a different move" is
  wrong advice for a winning move),
- a candidate the engine beats by more than the margin is REJECTED with
  the recommendation_rejected envelope, which never names the engine's move,
- the exact-move short-circuit accepts when the engine's free best IS the
  candidate,
- one margin regardless of standing, and a side ahead cannot submit a move
  that repeats the position,
- only a played-out move is taken; out of rounds (fallback) the best
  played-out move is, flagged when the engine had a stronger one.

A fixture-local fake emits `score mate` (the stock fakes only do cp) and
varies its reply by whether the `go` line carries `searchmoves` -- so the
free search and the candidate-restricted search can disagree.
"""
from __future__ import annotations

from pathlib import Path

import chess
import chess.engine
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play import tools_engine
from sturddle_view.play.engine_supervisor import EngineSupervisor
from sturddle_view.play.playbook import (
    MARGIN_LOSING,
    MARGIN_WINNING,
    PHASE_MIDDLEGAME,
    SOURCE_EVAL,
    Situation,
)
from sturddle_view.play.tools_engine import MAX_IDEAS, PlayOutRecord, make_recommend_move_tool

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


def _cp(centipawns: int) -> chess.engine.PovScore:
    # White-POV centipawn score, the shape _run_one_search results carry.
    return chess.engine.PovScore(chess.engine.Cp(centipawns), chess.WHITE)


def _tool(engine_path: str, board: chess.Board, situation: Situation | None = None):
    bus = EventBus()
    return make_recommend_move_tool(
        _launcher_from_path(engine_path, bus),
        bus=bus,
        board_provider=lambda: board,
        situation_provider=lambda: situation,
    )


def _situation(margin: str, repeats: tuple[str, ...] = ()) -> Situation:
    return Situation(margin, SOURCE_EVAL, PHASE_MIDDLEGAME, None, repeats)


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
    # +300 cp -- beats by 290, well over the default 20cp margin. Reject,
    # without naming the stronger move.
    engine_path = _make_free_vs_restricted_fake(
        tmp_path, "rm_reject",
        free_info="info depth 20 score cp 300 nodes 100 time 10 pv e2e4",
        restricted_info="info depth 20 score cp 10 nodes 100 time 10 pv g1f3",
    )
    board = chess.Board()
    tool = _tool(engine_path, board)
    out = await tool({"move": "Nf3", "depth": 20}, cancel_token=CancelToken())
    assert out.get("error") == "recommendation_rejected", out
    assert "stronger move exists" in out["reason"]
    assert "e4" not in out["reason"]
    assert "engine_best_san" not in out and "engine_best_move" not in out
    assert out["engine_best_score"]["score_cp"] == 300


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
    # Candidate +20, engine best (different move) +35: beats by 15, under
    # the 20cp margin. Cosmetic preference, not a blunder -- accept.
    engine_path = _make_free_vs_restricted_fake(
        tmp_path, "rm_margin",
        free_info="info depth 20 score cp 35 nodes 100 time 10 pv e2e4",
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


@pytest.mark.asyncio
async def test_shared_cache_makes_free_best_stable_across_calls(monkeypatch):
    # Flip-loop guard: the free "best" search is cached per turn, so a second
    # recommend_move in the same turn reuses it instead of re-searching. The
    # fake free search would return a DIFFERENT best on a re-run (e2e4 then
    # g1f3); the cache must prevent that re-run, so best stays e2e4 and a
    # resubmit of e4 (the named best) accepts via the exact-match short-circuit.
    free_calls = {"n": 0}
    flipping_best = ["e2e4", "g1f3"]  # would flip if the free search re-ran

    async def fake_search(engine_launcher, board, limit, *, root_moves=None, **kw):
        if root_moves:  # candidate-restricted (Search B): score the move low
            return {"depth": limit.depth, "score": _cp(10), "pv": list(root_moves)}, False
        # Free best (Search A): non-deterministic across calls -- but cached.
        uci = flipping_best[min(free_calls["n"], 1)]
        free_calls["n"] += 1
        return {"depth": limit.depth, "score": _cp(300), "pv": [chess.Move.from_uci(uci)]}, False

    monkeypatch.setattr(tools_engine, "_run_one_search", fake_search)
    cache = tools_engine.SearchCache()
    board = chess.Board()
    tool = tools_engine.make_recommend_move_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board, search_cache=cache,
    )

    # Call 1: recommend Nf3 -> free best is e2e4, Nf3 worse -> rejected.
    out1 = await tool({"move": "Nf3"}, cancel_token=CancelToken())
    assert out1.get("error") == "recommendation_rejected", out1

    # Call 2: submit e4. Free search is cached (not re-run), so best is
    # still e2e4 == the candidate -> exact-match accept, no flip.
    out2 = await tool({"move": "e4"}, cancel_token=CancelToken())
    assert out2.get("ok") is True, out2
    assert out2["uci"] == "e2e4"
    assert free_calls["n"] == 1, "free best must be searched once, then cached"


@pytest.mark.asyncio
async def test_book_move_accepted_without_search(monkeypatch):
    # The turn's book move is theory: accepted as-is, neither dominance
    # search runs. Any other move still gets both.
    searches = []

    async def fake_search(engine_launcher, board, limit, **kw):
        searches.append(limit.depth)
        return {"depth": limit.depth, "score": None, "pv": []}, False

    monkeypatch.setattr(tools_engine, "_run_one_search", fake_search)
    board = chess.Board()
    tool = tools_engine.make_recommend_move_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board,
        book_move_provider=lambda: "e2e4",
    )
    out = await tool({"move": "e4"}, cancel_token=CancelToken())
    assert out["ok"] is True and out["uci"] == "e2e4" and out["book"] is True
    assert searches == []
    out = await tool({"move": "d4"}, cancel_token=CancelToken())
    assert out["ok"] is True and "book" not in out
    assert len(searches) == 2


@pytest.mark.asyncio
async def test_illegal_move_returns_legal_moves_for_that_piece():
    # Screenshot regression: model kept guessing illegal knight moves (Nd7,
    # Ne7, Nd8...). An illegal recommend must hand back the legal moves for
    # the piece it meant, so the model picks a real one instead of guessing.

    # Black to move; the only black knight is on c3 -- Nd7 is illegal.
    board = chess.Board("r3r1k1/pp3pbp/1qp3p1/2B5/2BP2b1/Q1n2N2/P4PPP/3R1K1R b - - 3 17")
    tool = tools_engine.make_recommend_move_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board,
    )
    out = await tool({"move": "Nd7"}, cancel_token=CancelToken())
    assert out["error"] == "illegal_move", out
    # Exactly the c3-knight's legal destinations, no other piece's moves.
    expected = {
        board.san(m) for m in board.legal_moves
        if board.piece_at(m.from_square).piece_type == chess.KNIGHT
    }
    assert set(out["legal_moves"]) == expected
    assert "Nd7" not in out["legal_moves"]
    assert all(s.startswith("N") for s in out["legal_moves"])


@pytest.mark.asyncio
async def test_illegal_pawn_move_returns_pawn_moves():
    # A bare-square illegal pawn push reports pawn moves, not piece moves.
    board = chess.Board()  # white to move; e5 is illegal from startpos
    tool = tools_engine.make_recommend_move_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board,
    )
    out = await tool({"move": "e5"}, cancel_token=CancelToken())
    assert out["error"] == "illegal_move", out
    assert "e4" in out["legal_moves"]  # the legal one-square push
    assert all(s[0] in "abcdefgh" for s in out["legal_moves"])


@pytest.mark.asyncio
async def test_behind_does_not_widen_the_margin(tmp_path: Path):
    # Engine best beats the candidate by 70: the plan no longer buys slack,
    # so the side behind is held to the same margin.
    engine_path = _make_free_vs_restricted_fake(
        tmp_path, "rm_behind",
        free_info="info depth 20 score cp 90 nodes 100 time 10 pv e2e4",
        restricted_info="info depth 20 score cp 20 nodes 100 time 10 pv g1f3",
    )
    board = chess.Board()
    out = await _tool(engine_path, board, _situation(MARGIN_LOSING))(
        {"move": "Nf3", "depth": 20}, cancel_token=CancelToken(),
    )
    assert out.get("error") == "recommendation_rejected", out


@pytest.mark.asyncio
async def test_ahead_rejects_a_repeating_move_without_searching():
    # 1.Nf3 Nf6 2.Ng1 Ng8: 3.Nf3 repeats. Ahead, the gate vetoes it before
    # any search (the launcher would fail if reached).
    board = chess.Board()
    for uci in ("g1f3", "g8f6", "f3g1", "f6g8"):
        board.push_uci(uci)
    tool = tools_engine.make_recommend_move_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board,
        situation_provider=lambda: _situation(MARGIN_WINNING, repeats=("Nf3",)),
    )
    out = await tool({"move": "Nf3"}, cancel_token=CancelToken())
    assert out["error"] == "recommendation_rejected", out
    assert "repeats" in out["reason"]
    assert out["san"] == "Nf3"


# ---------- played-out rule and fallback ------------------------------

_E4 = chess.Move.from_uci("e2e4")
_NF3 = chess.Move.from_uci("g1f3")
_D4 = chess.Move.from_uci("d2d4")


def _scripted_search(free_uci: str, free_cp: int, restricted_cp: int, searches: list):
    """A _run_one_search stand-in: the free search names `free_uci` at
    `free_cp`; a restricted one scores its root move at `restricted_cp`."""
    async def fake_search(engine_launcher, board, limit, *, root_moves=None, **kw):
        searches.append(root_moves)
        if root_moves:
            return {
                "depth": limit.depth, "score": _cp(restricted_cp), "pv": list(root_moves),
            }, False
        return {
            "depth": limit.depth, "score": _cp(free_cp), "pv": [chess.Move.from_uci(free_uci)],
        }, False
    return fake_search


def _record(*first_moves: tuple[chess.Move, int]) -> PlayOutRecord:
    record = PlayOutRecord()
    for move, cp in first_moves:
        record.note_first_move(move, cp)
    return record


def _gated_tool(record: PlayOutRecord, *, fallback: bool = False, situation=None):
    board = chess.Board()
    return tools_engine.make_recommend_move_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board,
        play_out_record=record, fallback_provider=lambda: fallback,
        situation_provider=lambda: situation,
    )


@pytest.mark.asyncio
async def test_move_not_played_out_rejected_without_search(monkeypatch):
    searches: list = []
    monkeypatch.setattr(tools_engine, "_run_one_search", _scripted_search("e2e4", 30, 30, searches))
    out = await _gated_tool(_record((_E4, 30)))({"move": "Nf3"}, cancel_token=CancelToken())
    assert out["error"] == "recommendation_rejected", out
    assert "play_out" in out["reason"] and out["san"] == "Nf3"
    assert searches == []


@pytest.mark.asyncio
async def test_played_out_move_goes_to_the_dominance_check(monkeypatch):
    searches: list = []
    monkeypatch.setattr(tools_engine, "_run_one_search", _scripted_search("e2e4", 30, 25, searches))
    out = await _gated_tool(_record((_NF3, 25)))({"move": "Nf3"}, cancel_token=CancelToken())
    assert out.get("ok") is True and out["uci"] == "g1f3", out
    assert len(searches) == 2


def _full_record(*first_moves: tuple[chess.Move, int]) -> PlayOutRecord:
    record = _record(*first_moves)
    for n in range(MAX_IDEAS):
        record.admit(f"idea {n}")
    return record


@pytest.mark.parametrize("played", [False, True])
@pytest.mark.asyncio
async def test_rejection_opens_room_for_one_more_idea(monkeypatch, played):
    # Unplayed or dominated: either way the model must play out another idea.
    monkeypatch.setattr(tools_engine, "_run_one_search", _scripted_search("e2e4", 300, 10, []))
    record = _full_record(*([(_NF3, 10)] if played else []))
    out = await _gated_tool(record)({"move": "Nf3"}, cancel_token=CancelToken())
    assert out["error"] == "recommendation_rejected", out
    assert record.has_room("new idea")


@pytest.mark.asyncio
async def test_book_move_needs_no_play_out(monkeypatch):
    searches: list = []
    monkeypatch.setattr(tools_engine, "_run_one_search", _scripted_search("e2e4", 30, 30, searches))
    board = chess.Board()
    tool = tools_engine.make_recommend_move_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board,
        book_move_provider=lambda: "d2d4", play_out_record=PlayOutRecord(),
    )
    out = await tool({"move": "d4"}, cancel_token=CancelToken())
    assert out.get("ok") is True and out["book"] is True, out
    assert searches == []


@pytest.mark.asyncio
async def test_fallback_replaces_an_unplayed_move_with_the_best_played_out(monkeypatch):
    # d4 scored best after its first step, so it replaces the unplayed Nf3.
    searches: list = []
    monkeypatch.setattr(tools_engine, "_run_one_search", _scripted_search("d2d4", 40, 40, searches))
    record = _record((_E4, 10), (_D4, 40))
    out = await _gated_tool(record, fallback=True)({"move": "Nf3"}, cancel_token=CancelToken())
    assert out.get("ok") is True and out["uci"] == "d2d4", out
    assert "Nf3 was not played out" in out["note"]
    assert "weaker_than_best" not in out


@pytest.mark.asyncio
async def test_fallback_accepts_a_dominated_move_flagged(monkeypatch):
    searches: list = []
    monkeypatch.setattr(
        tools_engine, "_run_one_search", _scripted_search("e2e4", 300, 10, searches),
    )
    out = await _gated_tool(_record((_NF3, 10)), fallback=True)(
        {"move": "Nf3"}, cancel_token=CancelToken(),
    )
    assert out.get("ok") is True and out["uci"] == "g1f3", out
    assert out["weaker_than_best"] is True
    assert "practical choice" in out["note"] and "e4" not in out["note"]


@pytest.mark.asyncio
async def test_fallback_lifts_the_repetition_veto(monkeypatch):
    searches: list = []
    monkeypatch.setattr(tools_engine, "_run_one_search", _scripted_search("g1f3", 30, 30, searches))
    situation = _situation(MARGIN_WINNING, repeats=("Nf3",))
    held = await _gated_tool(_record((_NF3, 30)), situation=situation)(
        {"move": "Nf3"}, cancel_token=CancelToken(),
    )
    assert held["error"] == "recommendation_rejected" and "repeats" in held["reason"]
    out = await _gated_tool(_record((_NF3, 30)), fallback=True, situation=situation)(
        {"move": "Nf3"}, cancel_token=CancelToken(),
    )
    assert out.get("ok") is True, out


@pytest.mark.asyncio
async def test_fallback_with_nothing_played_out_still_rejects(monkeypatch):
    searches: list = []
    monkeypatch.setattr(tools_engine, "_run_one_search", _scripted_search("e2e4", 30, 30, searches))
    out = await _gated_tool(PlayOutRecord(), fallback=True)(
        {"move": "e4"}, cancel_token=CancelToken(),
    )
    assert out["error"] == "recommendation_rejected", out
    assert searches == []
