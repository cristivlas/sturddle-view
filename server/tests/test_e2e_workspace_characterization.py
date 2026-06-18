"""E2E characterization of openTournamentWorkspace's layout algorithms.

Pins the observable geometry contract of tile/tidy/snap BEFORE the
workspace controller is split into modules, so an extraction that moves
the layout math into its own file can be proven behavior-preserving.

These are not throwaway scaffolds: the geometry invariants asserted here
(no overlap, in-bounds, snap perfect-tiling + idempotency) are the real
contract the layout code must always honor.

Geometry is read off the WinBox DOM (.winbox inline styles), mapped to a
window key by its title text, mirroring how the other workspace e2e
tests read window state. No test-only production surface is added.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament.store import TournamentStore  # noqa: E402

from .conftest import (  # noqa: E402
    pin_arena_tournament_ux,
    run_uvicorn_subprocess,
    wait_perspective_ready,
)

# Title text WinBox renders for each system window; the only stable way
# to map a .winbox element back to its workspace key from the DOM.
TITLE_TO_KEY = {
    "Standings": "standings",
    "Live Games": "schedule",
    "Engine Instances": "engines",
    "Event Log": "log",
}
# Pixel slack absorbing WinBox's own sub-pixel rounding when we assert
# adjacency/coverage. Layout math floors to integers; a 2px cushion keeps
# the invariants meaningful without flaking on rounding.
ROUND_SLACK = 2


@pytest.fixture
def server(tmp_path):
    # Seed engine registry and one tournament on disk, then launch an
    # out-of-process server pointed at those paths (SV_* -> Settings).
    # tournament_fastchess_path is sys.executable (a real file detect_binary
    # echoes back), but fastchess is never spawned -- this test only opens
    # and lays out workspace windows.
    registry = EngineRegistry(path=tmp_path / "engines.json")
    # Seed a non-empty option_schema so listing engines doesn't lazily
    # probe sys.executable (not a real UCI engine) and log a handshake
    # timeout. The schema content is irrelevant -- layout never reads it.
    stub_schema = {"options": []}
    registry.add(name="engine-A", path=sys.executable, option_schema=stub_schema)
    registry.add(name="engine-B", path=sys.executable, option_schema=stub_schema)
    TournamentStore(tmp_path / "tournaments").create(
        name="alpha",
        template={"tc": "10+0.1"},
        engines=[{"name": "A", "cmd": "/bin/A"}, {"name": "B", "cmd": "/bin/B"}],
    )
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_ENGINE_REGISTRY_PATH": str(tmp_path / "engines.json"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


async def _goto_and_open(page, base):
    await pin_arena_tournament_ux(page)
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".tournament-row")
    await page.click(".tournament-row")
    await page.click(".tournaments-ribbon .t-workspace")
    await page.wait_for_function(
        "() => document.querySelectorAll('.winbox.sturddle-wb').length >= 1",
    )


async def _call_layout(page, method):
    """Invoke a layout verb through the public workspace API and wait a
    frame so WinBox geometry settles before we read it."""
    await page.evaluate(
        """async (method) => {
            const m = await import('/ui/app/tournament-workspace.js');
            m.getActiveWorkspace()[method]();
            await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        }""",
        method,
    )


async def _rects(page):
    """Map of window-key -> {x, y, w, h} for every open system window,
    read from the .winbox inline geometry. Live windows are excluded
    (they carry the sturddle-wb-live class)."""
    raw = await page.evaluate(
        """(titleMap) => {
            const out = {};
            for (const el of document.querySelectorAll('.winbox.sturddle-wb')) {
                if (el.classList.contains('sturddle-wb-live')) continue;
                const title = el.querySelector('.wb-title')?.textContent?.trim();
                const key = titleMap[title];
                if (!key) continue;
                out[key] = {
                    x: parseFloat(el.style.left),
                    y: parseFloat(el.style.top),
                    w: parseFloat(el.style.width),
                    h: parseFloat(el.style.height),
                };
            }
            return out;
        }""",
        TITLE_TO_KEY,
    )
    return raw


def _overlap_area(a, b):
    ox = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    oy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return ox * oy


def _assert_pairwise_disjoint(rects):
    keys = list(rects)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = rects[keys[i]], rects[keys[j]]
            # Allow a thin rounding seam but forbid genuine overlap.
            seam = ROUND_SLACK * max(a["w"], a["h"], b["w"], b["h"])
            assert _overlap_area(a, b) <= seam, (
                f"windows {keys[i]} and {keys[j]} overlap: {a} vs {b}"
            )


async def _viewport(page):
    return await page.evaluate(
        "() => ({ w: window.innerWidth, h: window.innerHeight })"
    )


@pytest.mark.asyncio
async def test_snap_perfect_tiling_and_idempotent(server, make_page):
    """snap() produces a gap-free, overlap-free tiling of the viewport,
    and a second snap() leaves every rect unchanged (the BSP partition
    claims edge-based cuts are idempotent)."""
    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)

    await _goto_and_open(page, server)
    # Open all four panels so snap has a non-trivial set to partition.
    await _call_layout(page, "tidy")
    await _call_layout(page, "snap")

    first = await _rects(page)
    assert len(first) == 4, f"expected 4 system windows, got {list(first)}"
    _assert_pairwise_disjoint(first)

    # Perfect tiling: the disjoint rects exactly fill their bounding box
    # (no interior gaps). Compares against the box the windows actually
    # occupy, so it needs no reconstruction of the workspace region math.
    covered = sum(r["w"] * r["h"] for r in first.values())
    bx0 = min(r["x"] for r in first.values())
    by0 = min(r["y"] for r in first.values())
    bx1 = max(r["x"] + r["w"] for r in first.values())
    by1 = max(r["y"] + r["h"] for r in first.values())
    box_area = (bx1 - bx0) * (by1 - by0)
    assert covered >= box_area * 0.98, (
        f"snap left interior gaps: covered {covered} of bounding box {box_area}"
    )

    await _call_layout(page, "snap")
    second = await _rects(page)
    for key, r in first.items():
        s = second[key]
        for dim in ("x", "y", "w", "h"):
            assert abs(r[dim] - s[dim]) <= ROUND_SLACK, (
                f"snap not idempotent for {key}.{dim}: {r[dim]} -> {s[dim]}"
            )

    assert errs == [], "JS errors:\n" + "\n".join(errs)


@pytest.mark.asyncio
async def test_tile_grid_in_bounds_no_overlap(server, make_page):
    """tile() lays windows on a grid: all within the workspace bounds,
    none overlapping (beyond a rounding seam)."""
    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)

    await _goto_and_open(page, server)
    await _call_layout(page, "tidy")   # open all four
    await _call_layout(page, "tile")

    rects = await _rects(page)
    assert len(rects) == 4
    _assert_pairwise_disjoint(rects)

    vp = await _viewport(page)
    for key, r in rects.items():
        assert r["x"] >= -ROUND_SLACK, f"{key} off left edge: {r}"
        assert r["y"] >= -ROUND_SLACK, f"{key} off top edge: {r}"
        assert r["x"] + r["w"] <= vp["w"] + ROUND_SLACK, f"{key} off right: {r}"
        assert r["y"] + r["h"] <= vp["h"] + ROUND_SLACK, f"{key} off bottom: {r}"

    assert errs == [], "JS errors:\n" + "\n".join(errs)


@pytest.mark.asyncio
async def test_tidy_opens_four_panels_and_places_them(server, make_page):
    """tidy() auto-opens all four system panels and assigns each its
    quadrant: engines top-left, standings top-right, schedule bottom-left,
    log bottom-right (the placements contract)."""
    _ctx, page = await make_page(viewport={"width": 1600, "height": 1000})
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.on("console", lambda m: errs.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)

    await _goto_and_open(page, server)
    await _call_layout(page, "tidy")

    rects = await _rects(page)
    assert set(rects) == {"engines", "standings", "schedule", "log"}, (
        f"tidy must open all four; got {set(rects)}"
    )
    _assert_pairwise_disjoint(rects)

    # Left column starts left of the right column.
    assert rects["engines"]["x"] < rects["standings"]["x"]
    assert rects["schedule"]["x"] < rects["log"]["x"]
    # Top row sits above the bottom row.
    assert rects["engines"]["y"] < rects["schedule"]["y"]
    assert rects["standings"]["y"] < rects["log"]["y"]

    assert errs == [], "JS errors:\n" + "\n".join(errs)
