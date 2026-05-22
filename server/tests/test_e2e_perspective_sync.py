"""End-to-end test: switching perspectives mid-game must restore the board.

Drives a real browser via Playwright. Skipped if Playwright or its Chromium
isn't available so unit-only test runs aren't blocked.
"""
from __future__ import annotations

import json

import chess
import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402


@pytest.fixture
def server(tmp_path):
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / "engines.json"),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


@pytest.mark.asyncio
async def test_play_perspective_remount_resyncs_state(server, page):
    """Inject a synthetic active game on the server, switch perspectives,
    and verify the remounted Play view receives a board_update with the
    correct FEN."""
    base = server

    # Build a non-trivial game state through the test-hooks endpoint.
    install_resp = httpx.post(
        f"{base}/_test/hve/install",
        json={
            "human_white": False,
            "moves_uci": ["e2e4", "c7c5"],
            "tc": {"initial_seconds": 300.0, "increment_seconds": 0.0},
            "white_time": 290.0,
            "black_time": 295.0,
            "game_id": "test-game",
        },
    )
    install_resp.raise_for_status()
    expected_fen = install_resp.json()["board_fen"]

    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)

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
