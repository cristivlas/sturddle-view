"""E2E: view-mode Analyze button gating at terminal positions.

The server rejects /game/analysis/start when the cursor position is
board-terminal (python-chess is_game_over). The client mirrors that
guard via FORCED_TERMINATIONS: #view-analyze disables at a forced
ending's final position and stays enabled everywhere else -- including
the last ply of a resigned game, which the server accepts.

Coverage splits by layer, so failures name their cause:
- UI (here): import a finished PGN, scrub the cursor, assert the button
  state (checkmate and stalemate parametrized; resigned pins the
  negative side);
- payload (here): a second /ws capture asserts the termination string
  of the exact event that drove the observed button state, so a
  python-chess rename fails by name, not just by a flipped button;
- membership: test_view_analyze_forced_set.py compares the client set
  verbatim to the enum-derived names in the default (non-e2e) suite.

Page errors are asserted in fixture TEARDOWN, so they surface alongside
a functional failure instead of being masked behind a green-path-only
trailing assert.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import sys

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

import pytest_asyncio  # noqa: E402

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)
from .forced_terminations import FORCED_PY  # noqa: E402

PLAY_PERSP = "#play-perspective"
VIEW_ANALYZE = "#view-analyze"
VIEW_BACK = "#view-back"
VIEW_LAST = "#view-last"
VIEW_CONTROLS = "#view-controls"

VIEWPORT = {"width": 1600, "height": 1000}

CHECKMATE_PGN = (
    '[Event "?"]\n[White "W"]\n[Black "B"]\n[Result "1-0"]\n\n'
    '1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7# 1-0\n'
)
# Sam Loyd's 10-move stalemate.
STALEMATE_PGN = (
    '[Event "?"]\n[White "W"]\n[Black "B"]\n[Result "1/2-1/2"]\n\n'
    '1. e3 a5 2. Qh5 Ra6 3. Qxa5 h5 4. Qxc7 Rah6 5. h4 f6 6. Qxd7+ Kf7 '
    '7. Qxb7 Qd3 8. Qxb8 Qh7 9. Qxc8 Kg6 10. Qe6 1/2-1/2\n'
)
RESIGNED_PGN = (
    '[Event "?"]\n[White "W"]\n[Black "B"]\n[Result "1-0"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 1-0\n'
)

ANALYZE_DISABLED = (
    f"() => document.querySelector('{VIEW_ANALYZE}').hasAttribute('disabled')"
)
ANALYZE_ENABLED = (
    f"() => !document.querySelector('{VIEW_ANALYZE}').hasAttribute('disabled')"
)

# Second /ws connection recording each board_update's view payload.
# Opened (and awaited open) after page load: it sees only click-driven
# events, so every assertion binds to a click, never the import's update.
CAPTURE_VIEW_EVENTS = """() => {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  window.__viewEvents = [];
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.addEventListener('message', (e) => {
    const v = JSON.parse(e.data)?.payload?.view;
    if (v) window.__viewEvents.push({
      cursor: v.cursor,
      game_over: !!v.game_over,
      termination: v.termination ?? null,
    });
  });
  return new Promise((res, rej) => {
    ws.addEventListener('open', () => res(true));
    // Reject on failure so the evaluate fails loudly instead of hanging
    // until the test timeout.
    ws.addEventListener('error', () => rej(new Error('capture ws failed')));
  });
}"""

# The newest captured view event -- the one that drove the current UI
# state. The capture socket can lag the app's by a broadcast hop, so
# waits below gate on ITS game_over before asserting on it.
LAST_VIEW_EVENT = "() => window.__viewEvents[window.__viewEvents.length - 1] ?? null"
LAST_VIEW_EVENT_GAME_OVER = f"() => ({LAST_VIEW_EVENT})()?.game_over === true"

CURSOR_AT_LAST_MOVE_CELL = """() => {
  const cells = [...document.querySelectorAll('.move-cell')];
  return cells.length > 0 && cells[cells.length - 1].classList.contains('is-current');
}"""


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name="FakeEngine", path=sys.executable)
    seed.select(e.id)
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(registry_path),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


@pytest_asyncio.fixture
async def checked_page(make_page):
    """Error-watched page factory. The empty-errors assert runs in
    teardown: pytest reports it as a distinct error next to any failed
    in-test assertion, so neither signal masks the other."""
    captures = []

    async def _make():
        _ctx, page = await make_page(viewport=VIEWPORT)
        captures.append((page, watch_page_errors(page)))
        return page

    yield _make
    for _page, errors in captures:
        assert errors == [], errors


def _import(base, text):
    resp = httpx.post(f"{base}/game/import", json={"text": text, "format": "pgn"})
    resp.raise_for_status()


async def _goto_view_mode(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP)
    await wait_perspective_ready(page)
    await page.wait_for_function(
        f"() => getComputedStyle(document.querySelector('{VIEW_CONTROLS}')).display !== 'none'",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pgn,termination", [
    (CHECKMATE_PGN, "checkmate"),
    (STALEMATE_PGN, "stalemate"),
])
async def test_analyze_disabled_only_at_forced_terminal_ply(server, checked_page, pgn, termination):
    base = server
    _import(base, pgn)
    page = await checked_page()
    await _goto_view_mode(page, base)
    assert await page.evaluate(CAPTURE_VIEW_EVENTS)

    # Import lands at the start position: enabled. Doubles as the noEngine
    # refutation -- an engineless disable would hold at every cursor, so
    # the terminal-ply disable below is attributable to the position gate.
    await page.wait_for_function(ANALYZE_ENABLED)

    # Jump to the terminal ply: disabled (server would reject the start).
    await page.click(VIEW_LAST)
    await page.wait_for_function(ANALYZE_DISABLED)

    # Pin the termination string of the event that produced the disabled
    # state (the newest one), so a python-chess enum rename fails by name.
    await page.wait_for_function(LAST_VIEW_EVENT_GAME_OVER)
    last = await page.evaluate(LAST_VIEW_EVENT)
    assert last["termination"] == termination, last

    # One ply back off the ending: enabled again.
    await page.click(VIEW_BACK)
    await page.wait_for_function(ANALYZE_ENABLED)


@pytest.mark.asyncio
async def test_analyze_enabled_at_resigned_last_ply(server, checked_page):
    base = server
    _import(base, RESIGNED_PGN)
    page = await checked_page()
    await _goto_view_mode(page, base)
    assert await page.evaluate(CAPTURE_VIEW_EVENTS)

    # Resignation is not board-terminal; the final position is analyzable.
    # Gate on the cursor having settled at the last move first; the button
    # state lands in the same (synchronous) handler, so a plain assert holds.
    await page.click(VIEW_LAST)
    await page.wait_for_function(CURSOR_AT_LAST_MOVE_CELL)
    assert await page.evaluate(ANALYZE_ENABLED)

    # Negative pin: the last ply IS game-over (PGN result), but its
    # termination must not be one the client treats as forced -- if the
    # server ever stamped a forced name here, fail by name.
    await page.wait_for_function(LAST_VIEW_EVENT_GAME_OVER)
    last = await page.evaluate(LAST_VIEW_EVENT)
    assert last["termination"] not in FORCED_PY, last
