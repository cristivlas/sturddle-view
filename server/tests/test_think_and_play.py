"""HumanVsEngine._think_and_play guards exercised without a real engine.

The full search lifecycle (analysis context, info pump, bestmove) needs
a real UCI subprocess and is covered by integration tests. Here we hit
the cheap, hard-edge guards: board-None early exit, ensure_engine
failure, the lock-re-entry consistency checks, and the opening-book
branch wiring (lookup gate, out-of-book latch, commit-without-search).
Book matching itself is pure logic covered by test_opening_lines."""
from __future__ import annotations


import chess
import pytest

from sturddle_view.events import EVT_ENGINE_SEARCH_START, EventBus
from sturddle_view.play import human_vs_engine as hve_mod
from sturddle_view.play.chess_clock import ChessClock, TimeControl
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.play.mode import Mode
from sturddle_view.play.opening_lines import BookRef

_BOOK = BookRef(path="book.pgn", plies=None, order=None, anchor=0)


@pytest.fixture
def hve():
    return HumanVsEngine(engine_path="/nonexistent", bus=EventBus())


def _play(board, *ucis):
    for u in ucis:
        board.push(chess.Move.from_uci(u))


def _arm(hve, book=_BOOK):
    """Minimal live-game state so _think_and_play passes its entry guard."""
    hve._board = chess.Board()
    hve._game_id = "test-game"
    hve._book = book


def _boom(message):
    async def raiser():
        raise RuntimeError(message)
    return raiser


def _engine_spy(hve, monkeypatch):
    """_ensure_engine failures are swallowed by design, so asserting 'the
    search path never ran' needs a call recorder, not a raiser."""
    called = []

    async def spy():
        called.append(True)
        raise RuntimeError("stop before real spawn")

    monkeypatch.setattr(hve, "_ensure_engine", spy)
    return called


def _forbidden(message):
    def raiser(*args):
        raise AssertionError(message)
    return raiser


async def test_think_and_play_returns_when_board_is_none(hve):
    """No active game → board is None → return without crash. Kills
    AddNot / `or`→`and` mutations on the
    `self._board is None or self._game_id is None` early-exit guard."""
    assert hve._board is None
    assert hve._game_id is None
    await hve._think_and_play()  # must not raise


async def test_think_and_play_swallows_ensure_engine_failure(hve, caplog, monkeypatch):
    """If `_ensure_engine` raises (engine binary missing, etc.) the
    coroutine logs and returns instead of propagating. Kills
    ExceptionReplacer mutations on the `except Exception` catch (a
    replacement non-parent class would let the exception escape)."""
    _arm(hve, book=None)
    monkeypatch.setattr(hve, "_ensure_engine", _boom("cannot start engine"))

    with caplog.at_level("ERROR", logger="sturddle_view.play.human_vs_engine"):
        await hve._think_and_play()  # must not raise

    assert any("could not start engine" in m for m in caplog.messages)


# ----- opening-book branch -----

async def test_book_hit_commits_without_search(hve, monkeypatch):
    _arm(hve)
    _play(hve._board, "e2e4")
    calls = {}

    def fake_reply(path, played, plies, order, anchor):
        calls["played"] = played
        return "e7e5"

    async def fake_commit(move, captured, game_id, gen):
        calls["move"] = move

    monkeypatch.setattr(hve_mod, "book_reply", fake_reply)
    monkeypatch.setattr(hve, "_commit_engine_move", fake_commit)
    searched = _engine_spy(hve, monkeypatch)
    events = await hve._bus.subscribe()
    await hve._think_and_play()
    assert calls["played"] == ["e2e4"]
    assert calls["move"] == chess.Move.from_uci("e7e5")
    assert not searched
    # The book branch blanks the live engine panel, so stale search info
    # (e.g. from before a takeback) never sits under an instant reply.
    kinds = [events.get_nowait().kind for _ in range(events.qsize())]
    assert EVT_ENGINE_SEARCH_START in kinds


async def test_book_miss_latches_out_of_book(hve, monkeypatch):
    _arm(hve)
    monkeypatch.setattr(hve_mod, "book_reply", lambda *args: None)
    monkeypatch.setattr(hve, "_ensure_engine", _boom("cannot start engine"))
    await hve._think_and_play()
    assert hve._out_of_book is True


async def test_latched_game_skips_book_lookup(hve, monkeypatch):
    _arm(hve)
    hve._out_of_book = True
    monkeypatch.setattr(
        hve_mod, "book_reply", _forbidden("book_reply must not run once latched"),
    )
    monkeypatch.setattr(hve, "_ensure_engine", _boom("cannot start engine"))
    await hve._think_and_play()


async def test_book_skipped_for_non_startpos_game(hve, monkeypatch):
    """EPD-seeded (or any FEN-seeded) games must not consult the PGN book,
    and must not latch out-of-book either."""
    _arm(hve)
    hve._start_fen = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1"
    monkeypatch.setattr(
        hve_mod, "book_reply", _forbidden("book_reply must not run for seeded games"),
    )
    monkeypatch.setattr(hve, "_ensure_engine", _boom("cannot start engine"))
    await hve._think_and_play()
    assert hve._out_of_book is False


async def test_takeback_clears_out_of_book_latch(hve):
    """Undo shortens the played prefix, so book lines that fell out of the
    pool may match again -- the latch must not survive a takeback."""
    _arm(hve)
    hve._mode = Mode.PLAY
    hve._human_white = True
    hve._clock = ChessClock(TimeControl(initial_seconds=60, increment_seconds=0))
    hve._clock.start_turn()
    for uci in ("e2e4", "e7e5"):
        hve._clock.append_snapshot()
        hve._board.push(chess.Move.from_uci(uci))
        hve._eval_history.append(None)
    hve._out_of_book = True
    await hve.takeback()
    assert len(hve._board.move_stack) == 0
    assert hve._out_of_book is False


async def test_stale_book_miss_does_not_latch_new_game(hve, monkeypatch):
    """A lookup that loses to a concurrent cancel/new-game (gen bump) must
    not latch the successor game out of book, and must not reach the
    search path either (the post-book lock re-checks the gen)."""
    _arm(hve)

    def miss_and_bump(*args):
        hve._think_gen += 1
        return None

    monkeypatch.setattr(hve_mod, "book_reply", miss_and_bump)
    searched = _engine_spy(hve, monkeypatch)
    await hve._think_and_play()
    assert hve._out_of_book is False
    assert not searched
