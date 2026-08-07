"""End-to-end tests for the custom player-name feature.

Covers:
  * Typing a name in Settings persists to the server settings store.
  * /game/new picks up the persisted name into the HVE snapshot.
  * The play-mode clock label reflects the name.
  * The exported PGN [White]/[Black] headers carry the name.
  * Export -> import round trip: the re-imported PGN's view-mode clock
    labels still show the custom name.
  * After a page reload the rehydrated game still surfaces the name.

Synchronization: all waits are deterministic. Engine-reply waits go through
``page.wait_for_function`` against server-published UI state (the move-list
DOM updates on each board_update) -- no time-based polling.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402

ENGINE_NAME = "MyEngine 1.0"
CUSTOM_NAME = "Alyssa P. Hacker"


def _make_fake_uci(root: Path, name: str) -> str:
    py = root / f"{name}.py"
    py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line: break\n"
        "    line = line.strip()\n"
        f"    if line == 'uci': sys.stdout.write('id name {name}\\nuciok\\n'); sys.stdout.flush()\n"
        "    elif line == 'isready': sys.stdout.write('readyok\\n'); sys.stdout.flush()\n"
        "    elif line.startswith('go'):\n"
        "        sys.stdout.write('bestmove e7e5\\n'); sys.stdout.flush()\n"
        "    elif line == 'quit': break\n"
    )
    if sys.platform.startswith("win"):
        wrapper = root / f"{name}.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{py}" %*\r\n')
        return str(wrapper)
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(py)


@pytest.fixture
def server(tmp_path):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    e = seed.add(name=ENGINE_NAME, path=_make_fake_uci(tmp_path, "FakeEngine"))
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


async def _open_app(page, base: str) -> None:
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)


async def _set_name_in_settings(page, name: str) -> None:
    """Open Settings, drive the wa-input value, commit via change event
    (PUT /settings), await the commit signal, close."""
    await page.click("#settings-btn")
    await page.locator("wa-tab[panel='play']").click()
    name_input = page.locator("wa-tab-panel[name='play'] wa-input").last
    await name_input.wait_for(state="visible")
    # wa-input is a custom element; Playwright's fill() can't drive it
    # directly. Set .value on the host (the property setter forwards to
    # the internal native input) then dispatch input/change. The change
    # handler PUTs to /settings and fires sturddle:settings-changed on
    # success -- await that so the server has committed before we return.
    await name_input.evaluate(
        f"""(el) => {{
            window.__nameSaved = new Promise((resolve) =>
                window.addEventListener('sturddle:settings-changed', resolve, {{ once: true }}));
            el.value = {name!r};
            el.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true, composed: true }}));
        }}"""
    )
    await page.evaluate("() => window.__nameSaved")
    await page.keyboard.press("Escape")


async def _click_new_game(page) -> None:
    btn = page.locator("#new-game")
    await btn.wait_for(state="visible")
    await btn.click()


async def _wait_for_bottom_label(page, expected: str) -> None:
    await page.wait_for_function(
        f"() => document.querySelector('.clock-name[data-side=\"bottom\"]')"
        f"?.textContent === {expected!r}"
    )


async def _wait_for_engine_reply(page) -> None:
    """Engine sends bestmove e7e5; the move-list DOM gains a second move cell
    on the resulting board_update. Pure UI-state wait -- no timer polling.
    """
    await page.wait_for_function(
        "() => document.querySelectorAll('.move-cell').length >= 2"
    )


@pytest.mark.asyncio
async def test_typing_name_in_settings_persists_to_server(server, page):
    """Typing the name in the Settings dialog must commit it to the
    server-side settings store (shared by all clients)."""
    base = server
    await _open_app(page, base)
    await _set_name_in_settings(page, CUSTOM_NAME)

    s = httpx.get(f"{base}/settings").json()
    assert s.get("player_name") == CUSTOM_NAME, s


@pytest.mark.asyncio
async def test_new_game_threads_custom_name_to_server_and_label(server, page):
    """User flow: set name in Settings, click New game -- the server snapshot
    and the bottom clock label must both reflect the custom name.
    """
    base = server
    await _open_app(page, base)
    await _set_name_in_settings(page, CUSTOM_NAME)
    await _click_new_game(page)
    await _wait_for_bottom_label(page, CUSTOM_NAME)

    st = httpx.get(f"{base}/_test/hve/state").json()
    assert st.get("player_name") == CUSTOM_NAME, st


@pytest.mark.asyncio
async def test_custom_name_survives_page_reload(server, page):
    """Set the name, start a game, play a move, refresh the page. The clock
    label must still show the custom name -- driven by the rehydrated server
    snapshot.
    """
    base = server
    await _open_app(page, base)
    await _set_name_in_settings(page, CUSTOM_NAME)
    await _click_new_game(page)
    await _wait_for_bottom_label(page, CUSTOM_NAME)

    httpx.post(f"{base}/game/move", json={"uci": "e2e4"}).raise_for_status()
    await _wait_for_engine_reply(page)

    await page.reload()
    await _open_app(page, base)

    st = httpx.get(f"{base}/_test/hve/state").json()
    assert st.get("player_name") == CUSTOM_NAME, f"server lost the name: {st}"
    await _wait_for_bottom_label(page, CUSTOM_NAME)


async def _play_and_export(page, base: str) -> str:
    """Open the app, start a human-white game with the custom name, play one
    move each side, and return the exported PGN text. The name goes through
    the settings API (the only path the server reads it from); human_white
    is forced via direct POST so we don't depend on the random side selection
    the New-game button would otherwise apply.
    """
    await _open_app(page, base)
    httpx.put(
        f"{base}/settings", json={"player_name": CUSTOM_NAME},
    ).raise_for_status()
    httpx.post(
        f"{base}/game/new", json={"human_side": "white"},
    ).raise_for_status()
    httpx.post(f"{base}/game/move", json={"uci": "e2e4"}).raise_for_status()
    await _wait_for_engine_reply(page)
    return httpx.get(f"{base}/game/pgn").text


@pytest.mark.asyncio
async def test_custom_name_in_exported_pgn(server, page):
    """The exported PGN must carry the custom name in the [White] header
    when the human plays White, and the engine's name in [Black].
    """
    base = server
    pgn = await _play_and_export(page, base)
    assert f'[White "{CUSTOM_NAME}"]' in pgn, pgn
    assert f'[Black "{ENGINE_NAME}"]' in pgn, pgn


@pytest.mark.asyncio
async def test_custom_name_roundtrip_export_import(server, page):
    """Full save/load loop: play with the custom name, export the PGN,
    import that exact text back -- the view-mode clock labels must show
    the custom name, proving the name survives the whole round trip
    through the PGN headers (view mode surfaces those, not the setting).
    """
    base = server
    pgn = await _play_and_export(page, base)
    # Save half: the exported PGN carries the custom name.
    assert f'[White "{CUSTOM_NAME}"]' in pgn, pgn

    # Load half: import the exact exported text and hard-reload into view
    # mode; the clock names must come from the PGN headers.
    httpx.post(
        f"{base}/game/import",
        json={"text": pgn, "format": "pgn"},
    ).raise_for_status()
    await page.reload()
    await _open_app(page, base)
    await page.wait_for_function(
        "() => getComputedStyle(document.querySelector('#view-controls'))"
        ".display !== 'none'",
    )

    names = await page.evaluate(
        """() => ({
            top: document.querySelector('.clock-name[data-side="top"]')?.textContent ?? null,
            bottom: document.querySelector('.clock-name[data-side="bottom"]')?.textContent ?? null,
        })"""
    )
    assert names["bottom"] == CUSTOM_NAME, (
        f"bottom clock name should be the custom name ({CUSTOM_NAME!r}), got {names['bottom']!r}"
    )
    assert names["top"] == ENGINE_NAME, (
        f"top clock name should be the engine ({ENGINE_NAME!r}), got {names['top']!r}"
    )
