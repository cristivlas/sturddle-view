"""E2E: multi-select in the New Tournament engine picker.

Covers the three desktop-standard selection modifiers in the available/
picked lists: plain click (replace), Ctrl+Click (toggle), Shift+Click
(range). Add → / ← Remove must operate on the full selection.

Skipped if Playwright / Chromium isn't available.
"""
from __future__ import annotations

import socket
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402
from sturddle_view.tournament.fastchess import FastchessRunner  # noqa: E402


ENGINE_NAMES = ["alpha", "beta", "gamma", "delta", "epsilon"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_fake_uci(root: Path, id_name: str) -> str:
    py = root / f"{id_name}.py"
    py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    line = line.strip()\n"
        f"    if line == 'uci': sys.stdout.write('id name {id_name}\\nuciok\\n'); sys.stdout.flush()\n"
        "    elif line == 'isready': sys.stdout.write('readyok\\n'); sys.stdout.flush()\n"
        "    elif line == 'quit': break\n"
    )
    if sys.platform.startswith("win"):
        wrapper = root / f"{id_name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        return str(wrapper)
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(py)


@pytest.fixture
def server(tmp_path, monkeypatch):
    # Pretend fastchess is present so the New tournament button is enabled.
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )

    import uvicorn

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    settings.tournament_root = str(tmp_path / "tournaments")
    settings.tournament_fastchess_path = sys.executable
    registry = EngineRegistry(path=tmp_path / "engines.json")
    for n in ENGINE_NAMES:
        registry.add(name=n, path=_make_fake_uci(tmp_path, n))
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


async def _open_new_tournament(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective", timeout=5000)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".tournaments-panel", timeout=5000)
    # New tournament button enables once fastchess + registry settle.
    await page.wait_for_function(
        "() => document.querySelector('.t-new') && !document.querySelector('.t-new').disabled",
        timeout=5000,
    )
    await page.click(".t-new")
    await page.locator(".new-tournament-form").first.wait_for(
        state="attached", timeout=5000,
    )
    await page.locator(".ne-available-list .ne-item").first.wait_for(
        state="attached", timeout=3000,
    )


async def _picked_names(page):
    return await page.evaluate(
        "() => Array.from(document.querySelectorAll('.ne-picked-list .ne-item'))"
        ".map(li => li.textContent)"
    )


async def _available_selected_names(page):
    return await page.evaluate(
        "() => Array.from(document.querySelectorAll('.ne-available-list .ne-item.selected'))"
        ".map(li => li.textContent)"
    )


@pytest.mark.asyncio
async def test_ctrl_click_toggles_and_add_moves_all(server, browser):
    """Ctrl+Click on two distinct rows selects both; Add moves both."""
    if browser is None:
        pytest.skip("chromium not installed")

    ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await ctx.new_page()
    try:
        await _open_new_tournament(page, server)
        # Plain click first item to seed.
        await page.locator(".ne-available-list .ne-item", has_text="alpha").click()
        # Ctrl+Click a non-adjacent third item.
        await page.locator(".ne-available-list .ne-item", has_text="gamma").click(modifiers=["Control"])
        names = await _available_selected_names(page)
        assert set(names) == {"alpha", "gamma"}, f"selection mismatch: {names}"
        await page.locator(".ne-add").click()
        picked = await _picked_names(page)
        assert picked == ["alpha", "gamma"], f"picked order/contents: {picked}"
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_shift_click_selects_range(server, browser):
    """Shift+Click extends selection from the last anchor to the clicked item."""
    if browser is None:
        pytest.skip("chromium not installed")

    ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await ctx.new_page()
    try:
        await _open_new_tournament(page, server)
        await page.locator(".ne-available-list .ne-item", has_text="beta").click()
        await page.locator(".ne-available-list .ne-item", has_text="delta").click(modifiers=["Shift"])
        names = await _available_selected_names(page)
        # Range beta..delta inclusive in the displayed (registry-add) order.
        assert names == ["beta", "gamma", "delta"], f"range mismatch: {names}"
        await page.locator(".ne-add").click()
        picked = await _picked_names(page)
        assert picked == ["beta", "gamma", "delta"], f"picked: {picked}"
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_plain_click_replaces_selection(server, browser):
    """A plain click without modifiers collapses any prior selection."""
    if browser is None:
        pytest.skip("chromium not installed")

    ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await ctx.new_page()
    try:
        await _open_new_tournament(page, server)
        await page.locator(".ne-available-list .ne-item", has_text="alpha").click()
        await page.locator(".ne-available-list .ne-item", has_text="gamma").click(modifiers=["Control"])
        # Plain click on a fourth row -> only that row is selected.
        await page.locator(".ne-available-list .ne-item", has_text="epsilon").click()
        names = await _available_selected_names(page)
        assert names == ["epsilon"], f"expected only epsilon selected, got {names}"
    finally:
        await ctx.close()


@pytest.mark.asyncio
async def test_remove_operates_on_multiselect(server, browser):
    """← Remove takes out every selected picked row at once."""
    if browser is None:
        pytest.skip("chromium not installed")

    ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await ctx.new_page()
    try:
        await _open_new_tournament(page, server)
        # Pick three engines first.
        await page.locator(".ne-available-list .ne-item", has_text="alpha").click()
        await page.locator(".ne-available-list .ne-item", has_text="gamma").click(modifiers=["Shift"])
        await page.locator(".ne-add").click()
        # Now multi-select two of the three in the picked pane and remove.
        await page.locator(".ne-picked-list .ne-item", has_text="alpha").click()
        await page.locator(".ne-picked-list .ne-item", has_text="gamma").click(modifiers=["Control"])
        await page.locator(".ne-remove").click()
        picked = await _picked_names(page)
        assert picked == ["beta"], f"expected only beta remaining, got {picked}"
    finally:
        await ctx.close()
