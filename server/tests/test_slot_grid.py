"""E2E unit-style tests for web/app/workspace-slot-grid.js.

Exercises the pure layout module directly via page.evaluate against a
served origin. No JS test framework in this repo, so we lean on
Playwright like the rest of the e2e tests.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402


VIEWPORT = {"width": 1280, "height": 720}
CELL_W = 200
CELL_H = 150
GAP = 8
# (1280 + 8) / 208 = 6.19 -> 6 cols; (720 + 8) / 158 = 4.61 -> 4 rows.
EXPECTED_COLS = 6
EXPECTED_ROWS = 4
EXPECTED_CAP = EXPECTED_COLS * EXPECTED_ROWS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
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
    yield f"http://127.0.0.1:{port}"
    s.should_exit = True
    s.force_exit = True
    thread.join(timeout=2)


async def _new_page(browser, base):
    ctx = await browser.new_context(viewport=VIEWPORT)
    page = await ctx.new_page()
    await page.goto(base + "/")
    return ctx, page


# Helper: run a function inside the page that builds a slot grid with
# the given fake-window list and returns the requested probe.
PROBE_JS = """
async ({windows, cellW, cellH, gap, probe}) => {
    const m = await import('/ui/app/workspace-slot-grid.js');
    const grid = m.createSlotGrid({
        top: 0, left: 0,
        getCellWidth: () => cellW,
        cellHeight: cellH,
        gap,
        getWindows: () => windows,
    });
    if (probe === 'claim') return grid.claim();
    if (probe === 'capacity') return grid.capacity();
    if (probe === 'rectAt0') return grid.rectAt(0);
    if (probe === 'rectAt1') return grid.rectAt(1);
    if (probe === 'rectAtCols') return grid.rectAt(%d);
    throw new Error('unknown probe: ' + probe);
}
""" % EXPECTED_COLS


async def _probe(page, *, windows, probe):
    return await page.evaluate(
        PROBE_JS,
        {"windows": windows, "cellW": CELL_W, "cellH": CELL_H, "gap": GAP, "probe": probe},
    )


@pytest.mark.asyncio
async def test_capacity_matches_viewport(server, browser):
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page = await _new_page(browser, server)
    try:
        cap = await _probe(page, windows=[], probe="capacity")
        assert cap == EXPECTED_CAP
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_empty_grid_claims_slot_zero(server, browser):
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page = await _new_page(browser, server)
    try:
        rect = await _probe(page, windows=[], probe="claim")
        assert rect == {"x": 0, "y": 0, "w": CELL_W, "h": CELL_H}
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_slot_zero_occupied_claims_slot_one(server, browser):
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page = await _new_page(browser, server)
    try:
        wins = [{"x": 0, "y": 0, "width": CELL_W, "height": CELL_H, "min": False}]
        rect = await _probe(page, windows=wins, probe="claim")
        assert rect == {"x": CELL_W + GAP, "y": 0, "w": CELL_W, "h": CELL_H}
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_only_slot_one_occupied_claims_slot_zero(server, browser):
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page = await _new_page(browser, server)
    try:
        wins = [{"x": CELL_W + GAP, "y": 0, "width": CELL_W, "height": CELL_H, "min": False}]
        rect = await _probe(page, windows=wins, probe="claim")
        assert rect == {"x": 0, "y": 0, "w": CELL_W, "h": CELL_H}
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_minimized_window_does_not_occupy(server, browser):
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page = await _new_page(browser, server)
    try:
        wins = [{"x": 0, "y": 0, "width": CELL_W, "height": CELL_H, "min": True}]
        rect = await _probe(page, windows=wins, probe="claim")
        assert rect == {"x": 0, "y": 0, "w": CELL_W, "h": CELL_H}
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_touching_edges_do_not_overlap(server, browser):
    """Window placed exactly to the right of slot 0 (sharing an edge with
    slot 1's left edge) must not register as occupying slot 0 -- shared
    edges count as touching, not overlapping."""
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page = await _new_page(browser, server)
    try:
        # Slot 0 spans x=[0, CELL_W); a window with x = CELL_W shares the
        # right edge of slot 0 but does not enter it.
        wins = [{"x": CELL_W, "y": 0, "width": 10, "height": 10, "min": False}]
        rect = await _probe(page, windows=wins, probe="claim")
        assert rect == {"x": 0, "y": 0, "w": CELL_W, "h": CELL_H}
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_full_grid_returns_null(server, browser):
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page = await _new_page(browser, server)
    try:
        # Construct an occupying window at every slot rect.
        wins = []
        for i in range(EXPECTED_CAP):
            col = i % EXPECTED_COLS
            row = i // EXPECTED_COLS
            wins.append({
                "x": col * (CELL_W + GAP),
                "y": row * (CELL_H + GAP),
                "width": CELL_W,
                "height": CELL_H,
                "min": False,
            })
        rect = await _probe(page, windows=wins, probe="claim")
        assert rect is None
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_rect_at_advances_to_next_row(server, browser):
    """Slot index = cols should land at row 1, col 0."""
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page = await _new_page(browser, server)
    try:
        rect = await _probe(page, windows=[], probe="rectAtCols")
        assert rect == {"x": 0, "y": CELL_H + GAP, "w": CELL_W, "h": CELL_H}
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_dynamic_cell_width_recomputes_capacity(server, browser):
    """Capacity must reflect the current getCellWidth() return value
    rather than a value frozen at construction."""
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page = await _new_page(browser, server)
    try:
        cap = await page.evaluate(
            """async () => {
                const m = await import('/ui/app/workspace-slot-grid.js');
                let w = 200;
                const grid = m.createSlotGrid({
                    top: 0, left: 0,
                    getCellWidth: () => w,
                    cellHeight: 150,
                    gap: 8,
                    getWindows: () => [],
                });
                const cap1 = grid.capacity();
                w = 400;
                const cap2 = grid.capacity();
                return { cap1, cap2 };
            }"""
        )
        # cap1: floor(1288/208) = 6 cols * 4 rows = 24
        # cap2: floor(1288/408) = 3 cols * 4 rows = 12
        assert cap["cap1"] == 24
        assert cap["cap2"] == 12
    finally:
        await ctx.close()
