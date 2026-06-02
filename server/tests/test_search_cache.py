"""SearchCache: cross-tool engine-search reuse within a turn.

The cache lets the engine-backed tools (analyze, top_moves, recommend_move,
the recommend-verifier) skip a search when one this turn already reached at
least the requested depth on the same position+restriction. These tests
monkeypatch the underlying `_run_one_search` with an in-process fake -- no
real engine, no subprocess -- so a reuse is observable as "the search fn was
not called", and the suite stays in milliseconds.

Pinned behaviors:
- same (fen, root_moves) at <= the reached depth reuses (no second call),
- a deeper request than what was reached re-runs,
- free vs candidate-restricted (root_moves) are distinct keys,
- a different position is a distinct key,
- clear() drops everything (the per-turn boundary),
- a cancelled search is never cached.
"""
from __future__ import annotations

import chess
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play import tools_engine
from sturddle_view.play.tools_engine import SearchCache


class _FakeSearch:
    """Stand-in for `_run_one_search`: returns a canned InfoDict at a fixed
    reached depth, counts invocations, and honors the cancel token (a
    cancelled call returns an empty info with cancelled=True, mirroring the
    real pump's early-out)."""

    def __init__(self, reached_depth: int = 6) -> None:
        self.calls = 0
        self.reached_depth = reached_depth

    async def __call__(
        self, engine_launcher, board, limit, *,
        bus, game_id, cancel_token, root_moves=None, settings_provider=None,
    ):
        self.calls += 1
        if cancel_token.cancelled:
            return {}, True
        return {"depth": self.reached_depth, "score": None, "pv": []}, False


@pytest.fixture
def _patched(monkeypatch):
    fake = _FakeSearch()
    monkeypatch.setattr(tools_engine, "_run_one_search", fake)
    return fake


def _limit(depth: int) -> chess.engine.Limit:
    return chess.engine.Limit(depth=depth)


async def _search(cache, board, depth, *, root_moves=None, token=None):
    return await cache.get_or_search(
        engine_launcher=lambda: None,
        board=board,
        limit=_limit(depth),
        bus=EventBus(),
        game_id="g",
        cancel_token=token or CancelToken(),
        root_moves=root_moves,
    )


@pytest.mark.asyncio
async def test_reuse_at_or_below_reached_depth(_patched, caplog):
    cache = SearchCache()
    board = chess.Board()
    await _search(cache, board, 6)
    with caplog.at_level("INFO", logger="sturddle_view.play.tools_engine"):
        info, cancelled = await _search(cache, board, 5)  # <= reached -> reuse
    assert _patched.calls == 1
    assert cancelled is False
    assert info.get("depth") == 6
    assert any("search cache hit" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_deeper_request_reruns(_patched):
    cache = SearchCache()
    board = chess.Board()
    await _search(cache, board, 6)
    await _search(cache, board, 20)  # reached 6 < 20 -> re-run
    assert _patched.calls == 2


@pytest.mark.asyncio
async def test_free_vs_restricted_are_distinct_keys(_patched):
    cache = SearchCache()
    board = chess.Board()
    move = chess.Move.from_uci("e2e4")
    await _search(cache, board, 6)
    await _search(cache, board, 6, root_moves=[move])
    assert _patched.calls == 2  # different root_moves -> different searches
    # ...each reuses on a repeat at its own key.
    await _search(cache, board, 6)
    await _search(cache, board, 6, root_moves=[move])
    assert _patched.calls == 2


@pytest.mark.asyncio
async def test_different_position_is_distinct_key(_patched):
    cache = SearchCache()
    start = chess.Board()
    after_e4 = chess.Board()
    after_e4.push_uci("e2e4")
    await _search(cache, start, 6)
    await _search(cache, after_e4, 6)
    assert _patched.calls == 2


@pytest.mark.asyncio
async def test_clocks_dont_split_key(_patched):
    # Same board, different halfmove/fullmove counters -> one key (epd()).
    cache = SearchCache()
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    a = chess.Board(fen)
    b = chess.Board(fen.replace("0 1", "9 42"))
    await _search(cache, a, 6)
    await _search(cache, b, 6)  # clocks differ, position same -> reuse
    assert _patched.calls == 1


@pytest.mark.asyncio
async def test_clear_drops_entries(_patched):
    cache = SearchCache()
    board = chess.Board()
    await _search(cache, board, 6)
    cache.clear()
    await _search(cache, board, 6)  # new turn -> must re-run
    assert _patched.calls == 2


@pytest.mark.asyncio
async def test_cancelled_search_not_cached(_patched):
    cache = SearchCache()
    board = chess.Board()
    cancelled_token = CancelToken()
    cancelled_token.cancel()
    _info, cancelled = await _search(cache, board, 6, token=cancelled_token)
    assert cancelled is True
    # A fresh request for the same key must run the search (not cached).
    await _search(cache, board, 6)
    assert _patched.calls == 2
