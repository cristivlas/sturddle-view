"""E2E: multi-select in the New Tournament engine picker.

Covers the three desktop-standard selection modifiers in the available/
picked lists: plain click (replace), Ctrl+Click (toggle), Shift+Click
(range). Add → / ← Remove must operate on the full selection.

Skipped if Playwright / Chromium isn't available.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402


ENGINE_NAMES = ["alpha", "beta", "gamma", "delta", "epsilon"]


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
def server(tmp_path):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    for n in ENGINE_NAMES:
        seed.add(name=n, path=_make_fake_uci(tmp_path, n))
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        # sys.executable is a real file, so detect_binary picks it up
        # and the "New tournament" button is enabled.
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
        "SV_ENGINE_REGISTRY_PATH": str(registry_path),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


async def _open_new_tournament(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(".tournaments-panel")
    # New tournament button enables once fastchess + registry settle.
    await page.wait_for_function(
        "() => document.querySelector('.t-new') && !document.querySelector('.t-new').disabled",
    )
    await page.click(".t-new")
    await page.locator(".new-tournament-form").first.wait_for(
        state="attached",
    )
    await page.locator(".ne-available-list .ne-item").first.wait_for(
        state="attached",
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
async def test_ctrl_click_toggles_and_add_moves_all(server, make_page):
    """Ctrl+Click on two distinct rows selects both; Add moves both."""
    _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
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


@pytest.mark.asyncio
async def test_shift_click_selects_range(server, make_page):
    """Shift+Click extends selection from the last anchor to the clicked item."""
    _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
    await _open_new_tournament(page, server)
    await page.locator(".ne-available-list .ne-item", has_text="beta").click()
    await page.locator(".ne-available-list .ne-item", has_text="delta").click(modifiers=["Shift"])
    names = await _available_selected_names(page)
    # Range beta..delta inclusive in the displayed (registry-add) order.
    assert names == ["beta", "gamma", "delta"], f"range mismatch: {names}"
    await page.locator(".ne-add").click()
    picked = await _picked_names(page)
    assert picked == ["beta", "gamma", "delta"], f"picked: {picked}"


@pytest.mark.asyncio
async def test_plain_click_replaces_selection(server, make_page):
    """A plain click without modifiers collapses any prior selection."""
    _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
    await _open_new_tournament(page, server)
    await page.locator(".ne-available-list .ne-item", has_text="alpha").click()
    await page.locator(".ne-available-list .ne-item", has_text="gamma").click(modifiers=["Control"])
    # Plain click on a fourth row -> only that row is selected.
    await page.locator(".ne-available-list .ne-item", has_text="epsilon").click()
    names = await _available_selected_names(page)
    assert names == ["epsilon"], f"expected only epsilon selected, got {names}"


@pytest.mark.asyncio
async def test_remove_operates_on_multiselect(server, make_page):
    """← Remove takes out every selected picked row at once."""
    _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
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
