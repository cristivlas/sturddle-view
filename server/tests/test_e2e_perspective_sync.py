"""End-to-end test: switching perspectives mid-game must restore the board.

Drives a real browser via Playwright. Skipped if Playwright or its Chromium
isn't available so unit-only test runs aren't blocked.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

playwright = pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    """Run uvicorn in a thread with isolated registry/settings; yield base URL."""
    import uvicorn

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="wsproto")
    s = uvicorn.Server(config)

    thread = threading.Thread(target=s.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not s.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    s.should_exit = True
    s.force_exit = True
    thread.join(timeout=2)


@pytest.mark.asyncio
async def test_play_perspective_remount_resyncs_state(server):
    """Inject a synthetic active game on the server, switch perspectives,
    and verify the remounted Play view receives a board_update with the
    correct FEN."""
    base, app = server

    # Build a non-trivial game state directly on the HVE so the test is
    # independent of any real UCI engine.
    import chess
    from sturddle_view.events import EventBus
    from sturddle_view.play.human_vs_engine import HumanVsEngine, TimeControl

    hve = HumanVsEngine(
        engine_path="/nonexistent",
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )
    hve._board = chess.Board()
    hve._board.push_uci("e2e4")
    hve._board.push_uci("c7c5")
    hve._human_white = False
    hve._tc = TimeControl(300.0, 0.0)
    hve._white_time = 290.0
    hve._black_time = 295.0
    hve._game_id = "test-game"
    hve._turn_started_at = time.monotonic()
    app.state.hve = hve

    expected_fen = hve._board.fen()

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as e:
            pytest.skip(f"chromium not installed: {e}")
        ctx = await browser.new_context()
        page = await ctx.new_page()
        try:
            await page.goto(base + "/")
            await page.wait_for_selector("#play-perspective", timeout=5000)
            await page.wait_for_timeout(500)

            # Capture WS frames *received after* we switch back.
            await page.evaluate(
                """() => {
                    window.__msgs = [];
                    const orig = WebSocket.prototype.send;
                    // Hook the existing socket via the framereceived isn't possible
                    // from page-side; instead, drain into __msgs by patching the
                    // event handler the app already installed.
                }"""
            )

            # Switch to Engines perspective.
            await page.click('button[data-perspective="engines"]')
            await page.wait_for_timeout(300)
            # Switch back; perspective mount calls /game/sync after 200ms.
            await page.click('button[data-perspective="play"]')
            await page.wait_for_timeout(1500)

            # Verify the rendered board matches the expected FEN.
            info = await page.evaluate(
                """() => {
                    const svg = document.querySelector('.game-view-board .board svg.cm-chessboard');
                    if (!svg) return {missing: true};
                    const pieces = [...svg.querySelectorAll('[data-piece]')]
                      .map(p => p.getAttribute('data-piece') + '@' + p.getAttribute('data-square'))
                      .sort();
                    return { pieces };
                }"""
            )
            assert not info.get("missing"), "board not mounted after switch back"
            # Build expected piece map from FEN.
            expected_board = chess.Board(expected_fen)
            expected_pieces = sorted(
                f"{('w' if expected_board.color_at(sq) == chess.WHITE else 'b')}"
                f"{chess.piece_symbol(expected_board.piece_at(sq).piece_type)}"
                f"@{chess.square_name(sq)}"
                for sq in chess.SQUARES
                if expected_board.piece_at(sq) is not None
            )
            assert info["pieces"] == expected_pieces, (
                f"board state after remount diverges from server.\n"
                f"  expected: {expected_pieces}\n"
                f"  actual:   {info['pieces']}"
            )
        finally:
            await browser.close()
