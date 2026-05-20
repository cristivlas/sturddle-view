"""E2E unit-style tests for web/app/workspace-slot-grid.js.

Exercises the pure layout module directly via page.evaluate against a
served origin. No JS test framework in this repo, so we lean on
Playwright like the rest of the e2e tests.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn  # noqa: E402


VIEWPORT = {"width": 1280, "height": 720}
CELL_W = 200
CELL_H = 150
GAP = 8
# (1280 + 8) / 208 = 6.19 -> 6 cols; (720 + 8) / 158 = 4.61 -> 4 rows.
EXPECTED_COLS = 6
EXPECTED_ROWS = 4
EXPECTED_CAP = EXPECTED_COLS * EXPECTED_ROWS


@pytest.fixture
def server(tmp_path):
    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with run_uvicorn(app) as (base, _s):
        yield base


async def _new_page(make_page, base):
    _ctx, page = await make_page(viewport=VIEWPORT)
    await page.goto(base + "/")
    return page


# Helper: run a function inside the page that builds a slot grid with
# the given fake-window list and returns the requested probe.
PROBE_JS = """
async ({windows, cellW, cellH, gap, probe, viewW, viewH}) => {
    const m = await import('/ui/app/workspace-slot-grid.js');
    const grid = m.createSlotGrid({
        top: 0, left: 0,
        getCellWidth: () => cellW,
        cellHeight: cellH,
        gap,
        getWindows: () => windows,
        getRight: () => viewW,
        getBottom: () => viewH,
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
        {"windows": windows, "cellW": CELL_W, "cellH": CELL_H, "gap": GAP, "probe": probe,
         "viewW": VIEWPORT["width"], "viewH": VIEWPORT["height"]},
    )


@pytest.mark.asyncio
async def test_capacity_matches_viewport(server, make_page):
    page = await _new_page(make_page, server)
    cap = await _probe(page, windows=[], probe="capacity")
    assert cap == EXPECTED_CAP


@pytest.mark.asyncio
async def test_empty_grid_claims_slot_zero(server, make_page):
    page = await _new_page(make_page, server)
    rect = await _probe(page, windows=[], probe="claim")
    assert rect == {"x": 0, "y": 0, "w": CELL_W, "h": CELL_H}


@pytest.mark.asyncio
async def test_slot_zero_occupied_claims_slot_one(server, make_page):
    page = await _new_page(make_page, server)
    wins = [{"x": 0, "y": 0, "width": CELL_W, "height": CELL_H, "min": False}]
    rect = await _probe(page, windows=wins, probe="claim")
    assert rect == {"x": CELL_W + GAP, "y": 0, "w": CELL_W, "h": CELL_H}


@pytest.mark.asyncio
async def test_only_slot_one_occupied_claims_slot_zero(server, make_page):
    page = await _new_page(make_page, server)
    wins = [{"x": CELL_W + GAP, "y": 0, "width": CELL_W, "height": CELL_H, "min": False}]
    rect = await _probe(page, windows=wins, probe="claim")
    assert rect == {"x": 0, "y": 0, "w": CELL_W, "h": CELL_H}


@pytest.mark.asyncio
async def test_minimized_window_does_not_occupy(server, make_page):
    page = await _new_page(make_page, server)
    wins = [{"x": 0, "y": 0, "width": CELL_W, "height": CELL_H, "min": True}]
    rect = await _probe(page, windows=wins, probe="claim")
    assert rect == {"x": 0, "y": 0, "w": CELL_W, "h": CELL_H}


@pytest.mark.asyncio
async def test_touching_edges_do_not_overlap(server, make_page):
    """Window placed exactly to the right of slot 0 (sharing an edge with
    slot 1's left edge) must not register as occupying slot 0 -- shared
    edges count as touching, not overlapping."""
    page = await _new_page(make_page, server)
    # Slot 0 spans x=[0, CELL_W); a window with x = CELL_W shares the
    # right edge of slot 0 but does not enter it.
    wins = [{"x": CELL_W, "y": 0, "width": 10, "height": 10, "min": False}]
    rect = await _probe(page, windows=wins, probe="claim")
    assert rect == {"x": 0, "y": 0, "w": CELL_W, "h": CELL_H}


@pytest.mark.asyncio
async def test_full_grid_returns_null(server, make_page):
    page = await _new_page(make_page, server)
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


@pytest.mark.asyncio
async def test_rect_at_advances_to_next_row(server, make_page):
    """Slot index = cols should land at row 1, col 0."""
    page = await _new_page(make_page, server)
    rect = await _probe(page, windows=[], probe="rectAtCols")
    assert rect == {"x": 0, "y": CELL_H + GAP, "w": CELL_W, "h": CELL_H}


@pytest.mark.asyncio
async def test_dynamic_cell_width_recomputes_capacity(server, make_page):
    """Capacity must reflect the current getCellWidth() return value
    rather than a value frozen at construction."""
    page = await _new_page(make_page, server)
    cap = await page.evaluate(
        """async ([viewW, viewH]) => {
            const m = await import('/ui/app/workspace-slot-grid.js');
            let w = 200;
            const grid = m.createSlotGrid({
                top: 0, left: 0,
                getCellWidth: () => w,
                cellHeight: 150,
                gap: 8,
                getWindows: () => [],
                getRight: () => viewW,
                getBottom: () => viewH,
            });
            const cap1 = grid.capacity();
            w = 400;
            const cap2 = grid.capacity();
            return { cap1, cap2 };
        }""",
        [VIEWPORT["width"], VIEWPORT["height"]],
    )
    # cap1: floor(1288/208) = 6 cols * 4 rows = 24
    # cap2: floor(1288/408) = 3 cols * 4 rows = 12
    assert cap["cap1"] == 24
    assert cap["cap2"] == 12
