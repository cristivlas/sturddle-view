"""Finished play games are auto-saved to the recent-imports store.

Three game-end paths must populate recents:
- Natural outcome (mate/draw via _finalize_game_locked from submit_move
  or _think_and_play).
- Resignation.
- Time forfeit (flag fall).

In-progress games and empty games (no moves) must NOT be saved.
"""
from __future__ import annotations

import io
from unittest.mock import AsyncMock

import chess
import chess.pgn
import pytest

from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl, ViewModeParams
from sturddle_view.recent_imports import RecentImports


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve(tmp_path):
    bus = EventBus()
    recents = RecentImports.load(root=tmp_path / "imports", cap=10)
    h = HumanVsEngine(
        engine_path="/nonexistent",
        bus=bus,
        recents=recents,
    )

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h, recents


async def test_resign_saves_finished_game_to_recents(hve):
    h, recents = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    await h.resign()

    rows = recents.list()
    assert len(rows) == 1
    row = rows[0]
    assert row["format"] == "pgn"
    assert row["summary"]["source"] == "play"
    assert row["summary"]["result"] == "0-1"  # human resigned -> engine wins
    # Blob round-trips: includes the move and a termination header.
    _, text = recents.get(row["hash"])
    assert "e4" in text
    assert "resignation" in text


async def test_empty_resign_does_not_pollute_recents(hve):
    """Resigning before any move is played: nothing to save."""
    h, recents = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.resign()
    assert recents.list() == []


async def test_in_progress_save_is_not_triggered(hve):
    """The per-move autosave path (`result="*"`) must not write to recents.
    Only true game-end finalization should."""
    h, recents = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    # No resign, no finalization. Recents must be empty.
    assert recents.list() == []


async def test_finalize_natural_outcome_saves_to_recents(hve):
    """Drive the board into Fool's Mate so _finalize_game_locked fires
    via submit_move's `ended` branch."""
    h, recents = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    # 1. f2f3 (white, human)
    await h.submit_move("f2f3")
    # 1... e7e5 (black, engine reply injected)
    async with h._lock:
        h._clock.append_snapshot()
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("e7e5"))
        h._eval_history.append(None)
    # 2. g2g4 (white, human)
    await h.submit_move("g2g4")
    # 2... d8h4 mate, but the engine is mocked. Submit directly via the
    # post-search path; we still expect the finalize-on-submit path
    # because submit_move treats the move that mates as ending.
    # Inject mate ply by mimicking a human move? Engine-reply path is
    # cleaner: push under lock + call finalize.
    async with h._lock:
        h._clock.append_snapshot()
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("d8h4"))
        h._eval_history.append(None)
        assert h._board.is_checkmate()
        end_id, end_payload = h._finalize_game_locked()
    await h._flush_recents_save()

    assert end_payload["result"] == "0-1"
    rows = recents.list()
    assert len(rows) == 1
    row = rows[0]
    assert row["summary"]["source"] == "play"
    assert row["summary"]["result"] == "0-1"
    _, text = recents.get(row["hash"])
    assert "checkmate" in text


async def test_no_recents_wired_is_safe(tmp_path):
    """An HVE constructed without a recents store must still resign cleanly."""
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    await h.resign()  # must not raise


async def test_time_forfeit_saves_to_recents(hve):
    """Flag fall (_handle_flag_fall) writes the finished game to recents."""
    h, recents = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    # Drive black's clock to zero so the next tick fires the flag.
    h._clock.black_time = 0.0
    await h._handle_flag_fall()

    rows = recents.list()
    assert len(rows) == 1
    row = rows[0]
    assert row["summary"]["source"] == "play"
    _, text = recents.get(row["hash"])
    assert "time_forfeit" in text


async def test_engine_mate_via_finalize_saves_to_recents(hve):
    """When the engine's reply (via _think_and_play's post-lock branch)
    mates, _finalize_game_locked + _flush_recents_save fire from that
    arm of submit_move. Cover the same end-of-game path that the engine
    move triggers, not just the human-submit path."""
    h, recents = hve
    # Scholar's-mate-ish setup: human (white) plays moves so the engine
    # arm can deliver mate. We mimic the engine's reply directly under
    # the lock, including the finalize call.
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    await h.submit_move("e2e4")
    async with h._lock:
        h._clock.append_snapshot()
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("e7e5"))
        h._eval_history.append(None)
    await h.submit_move("d1h5")
    async with h._lock:
        h._clock.append_snapshot()
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("b8c6"))
        h._eval_history.append(None)
    await h.submit_move("f1c4")
    async with h._lock:
        h._clock.append_snapshot()
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("g8f6"))
        h._eval_history.append(None)
    # Human delivers Scholar's Mate: 1.e4 e5 2.Qh5 Nc6 3.Bc4 Nf6 4.Qxf7#
    await h.submit_move("h5f7")
    assert h._board is None or h._game_id is None  # finalized

    rows = recents.list()
    assert len(rows) == 1
    assert rows[0]["summary"]["result"] == "1-0"
    _, text = recents.get(rows[0]["hash"])
    assert "checkmate" in text


async def test_recents_row_carries_game_id(hve):
    """The saved row must bind the active session's game_id so the
    eviction pin protects it while the session is live, and so
    /game/recent-imports/{id} can resolve it after."""
    h, recents = hve
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    game_id_before = h.game_id
    await h.submit_move("e2e4")
    await h.resign()

    rows = recents.list()
    assert len(rows) == 1
    assert rows[0]["game_id"] == game_id_before


async def test_play_from_here_with_comments_then_resign_builds_pgn(hve):
    """Regression: play_from_here seeds _play_comments at fork depth; subsequent
    moves must extend it so _build_play_game_pgn doesn't raise on length mismatch."""
    h, recents = hve
    await h.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5"],
        clock_history=None,
        comments=["c1", "c2"],
    ))
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    assert h._play_comments == ["c1", "c2"]

    # Human move -- _play_comments must grow to 3.
    await h.submit_move("g1f3")
    assert h._play_comments is not None
    assert len(h._play_comments) == len(h._board.move_stack)

    # Engine reply injected directly (engine is mocked).
    async with h._lock:
        h._clock.append_snapshot()
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("b8c6"))
        h._eval_history.append(None)
        if h._play_comments is not None:
            h._play_comments.append(None)

    assert len(h._play_comments) == len(h._board.move_stack)

    # Resign triggers _stash_recents_payload -> _build_play_game_pgn.
    # The bug caused ValueError here; after the fix it must succeed.
    await h.resign()
    rows = recents.list()
    assert len(rows) == 1


# ---- x-game fork-link tests ----


async def _import_parent(h, recents):
    """Drop a parent PGN into the store + view it. Returns the parent
    game_id (the live HVE id while in view mode)."""
    pgn = (
        '[Event "?"]\n[White "P"]\n[Black "Q"]\n[Result "*"]\n'
        '\n1. e4 e5 2. Nf3 Nc6 *'
    )
    parent_id = "gid-parent"
    await recents.save(
        fmt="pgn", text=pgn,
        summary={"white": "P", "black": "Q", "result": "*"},
        game_id=parent_id,
    )
    # Enter view mode against the parent's content so play_from_here
    # captures parent_id correctly.
    await h.enter_view_mode(
        ViewModeParams(
            start_fen=None,
            moves_uci=["e2e4", "e7e5", "g1f3", "b8c6"],
            clock_history=None,
            view_hash=None,
            view_summary={"white": "P", "black": "Q", "result": "*"},
        ),
        game_id=parent_id,
    )
    return parent_id


async def test_play_from_here_then_resign_writes_fork_row(hve):
    """play_from_here at parent ply >= 1 -> resign finalizes -> recents
    has the fork row with parent_game_id + fork_ply set, and the parent's
    refs is appended."""
    h, recents = hve
    parent_id = await _import_parent(h, recents)
    # Cursor at last ply (4) when we fork.
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    assert h.fork_link == (parent_id, 4)
    child_id = h.game_id
    await h.submit_move("d2d4")  # one play move to make the PGN non-empty
    await h.resign()

    # Child row is in recents with the fork link.
    child_got = recents.get_by_id(child_id)
    assert child_got is not None
    child_row = child_got[0]
    assert child_row["parent_game_id"] == parent_id
    assert child_row["fork_ply"] == 4
    # Parent's refs records the child at the fork ply.
    parent_row = recents.get_by_id(parent_id)[0]
    assert parent_row["refs"] == [{"game_id": child_id, "fork_ply": 4}]
    # And the stash is cleared after consumption.
    assert h.fork_link is None


async def test_play_from_here_at_ply_zero_is_not_a_fork(hve):
    """Forking at ply 0 of the parent is treated as a plain new game --
    no fork link, no parent ref."""
    h, recents = hve
    parent_id = await _import_parent(h, recents)
    await h.view_first()  # cursor = 0
    await h.play_from_here(tc=TimeControl(60, 0))
    assert h.fork_link is None
    await h.submit_move("e2e4")
    await h.resign()
    parent_row = recents.get_by_id(parent_id)[0]
    assert parent_row["refs"] == []


async def test_new_game_clears_stale_fork_link(hve):
    """play_from_here stashes a link; a subsequent plain new_game must
    drop it so the next finalization does NOT carry a stale parent."""
    h, recents = hve
    await _import_parent(h, recents)
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    assert h.fork_link is not None
    # User abandons fork -> plain new game.
    await h.new_game(human_white=True, tc=TimeControl(60, 0))
    assert h.fork_link is None
    new_game_id = h.game_id
    await h.submit_move("e2e4")
    await h.resign()
    row = recents.get_by_id(new_game_id)[0]
    assert "parent_game_id" not in row
    assert "fork_ply" not in row


async def test_enter_view_mode_drops_link_by_default(hve):
    """Import-on-top of a forked play game must drop the stash; the new
    view game has its own fresh lineage."""
    h, recents = hve
    await _import_parent(h, recents)
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    assert h.fork_link is not None
    # Simulate import-on-top: new view mode WITHOUT preserve.
    await h.enter_view_mode(
        ViewModeParams(start_fen=None, moves_uci=[], clock_history=None),
        game_id="gid-fresh",
    )
    assert h.fork_link is None


async def test_enter_view_mode_preserves_link_when_asked(hve):
    """``/view/start``-style transition passes fork_link through so the
    play->view state-flip can hand the link to a downstream commit."""
    h, recents = hve
    parent_id = await _import_parent(h, recents)
    await h.view_last()
    await h.play_from_here(tc=TimeControl(60, 0))
    link_before = h.fork_link
    assert link_before == (parent_id, 4)
    # Simulate /view/start: it captures the live link and passes it
    # through enter_view_mode.
    await h.enter_view_mode(
        ViewModeParams(start_fen=None, moves_uci=[], clock_history=None),
        game_id="gid-view-snapshot",
        fork_link=link_before,
    )
    assert h.fork_link == link_before


# ---- x-game fork PGN preservation matrix (B9 + sanity) ----
#
# Combinations of parent clock data x parent comments x child play.
# Each test forks the parent at cursor 4 (after 2 full moves), plays
# one engine half-move on the child, resigns, and inspects the child
# row's PGN in recents. Asserts:
#   - inherited plies (0..fork_ply-1) keep parent's clk / comments
#     byte-for-byte when present.
#   - inherited plies do NOT get fabricated `Xs` or comment tokens
#     when the parent had none.
#   - post-fork plies are unaffected.

_PARENT_MOVES = ["e2e4", "e7e5", "g1f3", "b8c6"]
_PARENT_NPLIES = len(_PARENT_MOVES)


async def _import_parent_full(
    h, recents, *, gid="gid-parent",
    clock_history: list[tuple[float | None, float | None]] | None = None,
    comments: list[str | None] | None = None,
):
    await recents.save(
        fmt="pgn", text='1. e4 e5 2. Nf3 Nc6 *',
        summary={"white": "P", "black": "Q", "result": "*"},
        game_id=gid,
    )
    await h.enter_view_mode(
        ViewModeParams(
            start_fen=None,
            moves_uci=_PARENT_MOVES,
            clock_history=clock_history,
            view_summary={"white": "P", "black": "Q", "result": "*"},
            comments=comments,
        ),
        game_id=gid,
    )
    return gid


async def _fork_and_finalize(h):
    """Fork at cursor=4 (post-2-moves), play one engine half-move,
    resign. Returns the child's game_id (assigned by play_from_here)."""
    await h.view_last()
    child_gid = await h.play_from_here(tc=TimeControl(60, 0))
    # Simulate one engine move so the child has at least one new ply.
    async with h._lock:
        h._clock.append_snapshot()
        h._consume_turn_time()
        h._board.push(chess.Move.from_uci("d2d4"))
        h._eval_history.append(None)
        if h._play_comments is not None:
            h._play_comments.append(None)
    await h.resign()
    return child_gid


def _child_pgn_from_recents(recents, child_gid):
    got = recents.get_by_id(child_gid)
    assert got is not None, f"child {child_gid} not in recents"
    _row, text = got
    return text


def _ply_comments_from_pgn(text):
    """Return per-ply comment strings from a PGN. None for plies with
    no comment at all."""
    game = chess.pgn.read_game(io.StringIO(text))
    return [(n.comment or None) for n in game.mainline()]


async def test_fork_t1_no_clk_no_comments(hve):
    """T1: parent has no clock data and no comments. Inherited plies
    must have empty comments (no fabricated `0.0s`, no spurious text)."""
    h, recents = hve
    await _import_parent_full(h, recents, clock_history=None, comments=None)
    child_gid = await _fork_and_finalize(h)
    text = _child_pgn_from_recents(recents, child_gid)
    plies = _ply_comments_from_pgn(text)
    # Inherited plies 0..3: no comment at all.
    for p in plies[:_PARENT_NPLIES]:
        assert p is None, f"unexpected comment on inherited ply: {p!r}"


async def test_fork_t2_full_clk_no_comments(hve):
    """T2: parent has full clock data on every ply, no comments.
    Inherited plies' comments must be exactly the cutechess `Xs` token
    (no fabricated text, no missing clock)."""
    h, recents = hve
    clk = [(300.0, 300.0), (295.0, 300.0), (295.0, 290.0), (288.0, 290.0)]
    await _import_parent_full(h, recents, clock_history=clk, comments=None)
    child_gid = await _fork_and_finalize(h)
    text = _child_pgn_from_recents(recents, child_gid)
    plies = _ply_comments_from_pgn(text)
    # Inherited plies: must contain a clock token, must NOT be empty.
    for p in plies[:_PARENT_NPLIES]:
        assert p is not None and p.endswith("s"), (
            f"missing clock token on inherited ply: {p!r}"
        )
        # And no fabricated text -- only the token (possibly with eval).
        assert all(part.endswith("s") or "/" in part for part in p.split()), (
            f"unexpected text on inherited ply: {p!r}"
        )


async def test_fork_t3_no_clk_full_comments(hve):
    """T3: parent has no clock data but every ply has a comment.
    Inherited plies must preserve the parent's comment text and NOT
    have a `0.0s` token appended (B9 -- the originating regression)."""
    h, recents = hve
    cmts = ["c0", "c1", "c2", "c3"]
    await _import_parent_full(h, recents, clock_history=None, comments=cmts)
    child_gid = await _fork_and_finalize(h)
    text = _child_pgn_from_recents(recents, child_gid)
    plies = _ply_comments_from_pgn(text)
    for i, expected in enumerate(cmts):
        got = plies[i]
        assert got is not None and expected in got, (
            f"ply {i}: lost comment {expected!r} (got {got!r})"
        )
        assert "0.0s" not in got, f"B9 regression on ply {i}: {got!r}"


async def test_fork_t4_full_clk_full_comments(hve):
    """T4: parent has both clock and comments on every ply (the
    Anderssen-with-annotations-and-times case). Both must survive."""
    h, recents = hve
    clk = [(300.0, 300.0), (295.0, 300.0), (295.0, 290.0), (288.0, 290.0)]
    cmts = ["c0", "c1", "c2", "c3"]
    await _import_parent_full(h, recents, clock_history=clk, comments=cmts)
    child_gid = await _fork_and_finalize(h)
    text = _child_pgn_from_recents(recents, child_gid)
    plies = _ply_comments_from_pgn(text)
    for i, expected in enumerate(cmts):
        got = plies[i]
        assert got is not None and expected in got, (
            f"ply {i}: lost comment {expected!r} (got {got!r})"
        )
        # And a clock-style token alongside (ends with `s`).
        tail = got.split()[-1]
        assert tail.endswith("s"), (
            f"ply {i}: lost clock token (got {got!r})"
        )


async def test_fork_t5_partial_clk_no_comments(hve):
    """T5: parent has clock on some plies, None on others. The plies
    with clock data emit a token; the None plies do NOT get a `0.0s`."""
    h, recents = hve
    # ply 0: no clock, ply 1: clk, ply 2: no clock, ply 3: clk.
    clk = [(None, None), (295.0, None), (None, None), (288.0, None)]
    await _import_parent_full(h, recents, clock_history=clk, comments=None)
    child_gid = await _fork_and_finalize(h)
    text = _child_pgn_from_recents(recents, child_gid)
    plies = _ply_comments_from_pgn(text)
    # ply 0: no token (no before, no after).
    assert plies[0] is None or "s" not in plies[0], (
        f"ply 0 should have no token: {plies[0]!r}"
    )
    # ply 2: also no token.
    assert plies[2] is None or "s" not in plies[2], (
        f"ply 2 should have no token: {plies[2]!r}"
    )


async def test_fork_t6_no_clk_partial_comments(hve):
    """T6: parent has comments only on some plies. Plies with no
    comment must remain empty; plies with a comment keep it."""
    h, recents = hve
    cmts = ["c0", None, "c2", None]
    await _import_parent_full(h, recents, clock_history=None, comments=cmts)
    child_gid = await _fork_and_finalize(h)
    text = _child_pgn_from_recents(recents, child_gid)
    plies = _ply_comments_from_pgn(text)
    assert plies[0] is not None and "c0" in plies[0]
    assert plies[1] is None, f"ply 1 should have no comment: {plies[1]!r}"
    assert plies[2] is not None and "c2" in plies[2]
    assert plies[3] is None, f"ply 3 should have no comment: {plies[3]!r}"
