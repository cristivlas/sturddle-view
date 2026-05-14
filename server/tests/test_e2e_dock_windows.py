"""E2E: Play-perspective debug windows dock/undock/restore lifecycle.

Covers the UCI Log and Search Lines windows refactored into the
createDockableWindow factory (web/app/play-debug-windows.js).
Skipped if Playwright is missing.
"""
from __future__ import annotations

import socket
import sys
import threading
import time

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402


PLAY_PERSP = "#play-perspective"
DOCK_LEFT = ".play-dock-left"
PV_BTN = "#pv-table-btn"
UCI_BTN = "#uci-log-btn"
UCI_WB = ".winbox.sturddle-wb-uci-log"
PV_WB = ".winbox.sturddle-wb-pvtable"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_server(tmp_path):
    import uvicorn

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    settings.tournament_root = str(tmp_path / "tournaments")
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
    return f"http://127.0.0.1:{port}", s, thread


@pytest.fixture
def server(tmp_path):
    base, s, thread = _spawn_server(tmp_path)
    yield base
    s.should_exit = True
    s.force_exit = True
    thread.join(timeout=2)


async def _new_page(browser):
    ctx = await browser.new_context(viewport={"width": 1600, "height": 1000})
    page = await ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return ctx, page, errors


def _assert_no_errors(errors):
    benign = ("Failed to load resource",)
    real = [e for e in errors if not any(b in e for b in benign)]
    assert real == [], "JS errors:\n" + "\n".join(real)


async def _goto_play(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector(PLAY_PERSP, timeout=5000)


async def _snapshot(page):
    return await page.evaluate("""
      () => {
        const dock = document.querySelector('.play-dock-left');
        const slots = Array.from(document.querySelectorAll('.play-dock-left .dock-slot')).map(s => ({
          title: s.querySelector('.dock-slot-title')?.textContent,
          hasBody: !!s.querySelector('.dock-slot-body')?.firstElementChild,
        }));
        const wbs = Array.from(document.querySelectorAll('.winbox.sturddle-wb')).map(w => ({
          cls: w.className,
          title: w.querySelector('.wb-title')?.textContent,
        }));
        return {
          dockEmpty: dock?.classList.contains('dock-empty') ?? null,
          slots,
          wbs,
          ucilogOpen: localStorage.getItem('sturddle.ucilog.open'),
          pvtableOpen: localStorage.getItem('sturddle.pvtable.open'),
          ucilogDocked: localStorage.getItem('sturddle.ucilog.docked'),
          pvtableDocked: localStorage.getItem('sturddle.pvtable.docked'),
        };
      }
    """)


async def _slot_titles(page):
    s = await _snapshot(page)
    return [x["title"] for x in s["slots"]]


async def _click_slot_undock(page, title):
    await page.evaluate(
        "(t) => { const s = [...document.querySelectorAll('.dock-slot')]"
        ".find(x => x.querySelector('.dock-slot-title')?.textContent === t);"
        " s?.querySelector('.dock-slot-undock')?.click(); }",
        title,
    )


async def _click_wb_dock(page, wb_class):
    await page.evaluate(
        "(c) => document.querySelector('.winbox.' + c + ' .wb-dock-ctrl')?.click()",
        wb_class,
    )


@pytest.mark.asyncio
async def test_default_docked_on_first_open(server, browser):
    """Both windows dock by default; Search Lines slot sits above UCI Log."""
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play(page, server)
        await page.click(PV_BTN)
        await page.click(UCI_BTN)
        await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot", timeout=2000)
        titles = await _slot_titles(page)
        assert titles == ["Search Lines", "UCI Log"], titles
        s = await _snapshot(page)
        assert s["ucilogOpen"] == "1" and s["pvtableOpen"] == "1"
        assert s["wbs"] == []
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_undock_via_slot_button(server, browser):
    """Clicking the slot's undock control opens a WinBox and removes the slot."""
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play(page, server)
        await page.click(UCI_BTN)
        await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot", timeout=2000)
        await _click_slot_undock(page, "UCI Log")
        await page.wait_for_selector(UCI_WB, timeout=2000)
        s = await _snapshot(page)
        assert not any(x["title"] == "UCI Log" for x in s["slots"])
        assert s["ucilogDocked"] == "0"
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_redock_via_winbox_control(server, browser):
    """The WinBox dock control returns the window to its slot in correct order."""
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play(page, server)
        await page.click(PV_BTN)
        await page.click(UCI_BTN)
        await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot", timeout=2000)
        await _click_slot_undock(page, "UCI Log")
        await page.wait_for_selector(UCI_WB, timeout=2000)
        await _click_wb_dock(page, "sturddle-wb-uci-log")
        await page.wait_for_function(
            "() => !document.querySelector('.winbox.sturddle-wb-uci-log')",
            timeout=2000,
        )
        titles = await _slot_titles(page)
        assert titles == ["Search Lines", "UCI Log"], titles
        s = await _snapshot(page)
        assert s["ucilogDocked"] == "1"
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_close_via_ribbon_tears_down(server, browser):
    """Toggling the ribbon button while open closes the window and clears state."""
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play(page, server)
        await page.click(PV_BTN)
        await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot", timeout=2000)
        await page.click(PV_BTN)  # toggle off
        await page.wait_for_function(
            f"() => !document.querySelector('{DOCK_LEFT} .dock-slot')",
            timeout=2000,
        )
        s = await _snapshot(page)
        assert s["slots"] == []
        assert s["dockEmpty"] is True
        assert s["pvtableOpen"] == "0"
        _assert_no_errors(errors)
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_nav_away_and_back_restores(server, browser):
    """closeDebugWindows on unmount, restoreDebugWindows on remount."""
    if browser is None:
        pytest.skip("chromium not installed")
    ctx, page, errors = await _new_page(browser)
    try:
        await _goto_play(page, server)
        await page.click(PV_BTN)
        await page.click(UCI_BTN)
        await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot", timeout=2000)

        # Nav away to engines -- Play perspective unmounts, dock element gone.
        await page.click('button[data-perspective="engines"]')
        await page.wait_for_function(
            "() => !document.querySelector('.play-dock-left')",
            timeout=2000,
        )
        s = await _snapshot(page)
        assert s["slots"] == []
        # open flags remain so restore knows to reopen
        assert s["ucilogOpen"] == "1" and s["pvtableOpen"] == "1"

        # Nav back -- both windows restored, docked, in correct order.
        await page.click('button[data-perspective="play"]')
        await page.wait_for_selector(f"{DOCK_LEFT} .dock-slot", timeout=2000)
        titles = await _slot_titles(page)
        assert titles == ["Search Lines", "UCI Log"], titles
        _assert_no_errors(errors)
    finally:
        await ctx.close()
