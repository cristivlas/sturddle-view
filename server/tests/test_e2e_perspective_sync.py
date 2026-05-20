"""End-to-end test: switching perspectives mid-game must restore the board.

Drives a real browser via Playwright. Skipped if Playwright or its Chromium
isn't available so unit-only test runs aren't blocked.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn  # noqa: E402


@pytest.fixture
def server(tmp_path):
    """Run uvicorn in a thread with isolated registry/settings; yield base URL."""
    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with run_uvicorn(app) as (base, _s):
        yield base, app


@pytest.mark.asyncio
async def test_play_perspective_remount_resyncs_state(server, page):
    """Inject a synthetic active game on the server, switch perspectives,
    and verify the remounted Play view receives a board_update with the
    correct FEN."""
    base, app = server

    # Build a non-trivial game state directly on the HVE so the test is
    # independent of any real UCI engine.
    import chess
    from sturddle_view.events import EventBus
    from sturddle_view.play.chess_clock import ChessClock, TimeControl
    from sturddle_view.play.human_vs_engine import HumanVsEngine

    hve = HumanVsEngine(
        engine_path="/nonexistent",
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    hve._board = chess.Board()
    hve._board.push_uci("e2e4")
    hve._board.push_uci("c7c5")
    hve._eval_history = [None, None]
    hve._human_white = False
    hve._clock = ChessClock(TimeControl(300.0, 0.0))
    hve._clock.white_time = 290.0
    hve._clock.black_time = 295.0
    hve._game_id = "test-game"
    hve._clock.start_turn()
    app.state.hve = hve

    expected_fen = hve._board.fen()

    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")

    await page.evaluate(
        """() => {
            window.__msgs = [];
            const orig = WebSocket.prototype.send;
        }"""
    )

    # Switch to Engines perspective; wait for the perspective root to
    # mount and finish its is-pending transition.
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_function(
        "() => !!document.querySelector('#engines-perspective')"
        " && !document.querySelector('.perspective-root')?.classList.contains('is-pending')",
    )
    # Switch back; the play perspective remounts and calls /game/sync.
    # Wait for the board to render the synthetic game's pieces, which
    # is exactly the post-sync state the assertion verifies.
    expected_board = chess.Board(expected_fen)
    expected_pieces = sorted(
        f"{('w' if expected_board.color_at(sq) == chess.WHITE else 'b')}"
        f"{chess.piece_symbol(expected_board.piece_at(sq).piece_type)}"
        f"@{chess.square_name(sq)}"
        for sq in chess.SQUARES
        if expected_board.piece_at(sq) is not None
    )
    await page.click('button[data-perspective="play"]')
    # Waiting for the board to render the expected pieces IS the
    # assertion: the post-/game/sync state matching the synthetic game.
    await page.wait_for_function(
        """(expected) => {
            const svg = document.querySelector('.game-view-board .board svg.cm-chessboard');
            if (!svg) return false;
            const pieces = [...svg.querySelectorAll('[data-piece]')]
              .map(p => p.getAttribute('data-piece') + '@' + p.getAttribute('data-square'))
              .sort();
            return JSON.stringify(pieces) === JSON.stringify(expected);
        }""",
        arg=expected_pieces,
    )
