"""End-to-end tests for the custom player-name feature.

Covers:
  * Typing a name in Settings persists to localStorage.
  * /game/new threads it into the server's HVE snapshot.
  * The play-mode clock label reflects the name.
  * The exported PGN [White]/[Black] headers carry the name.
  * After a page reload the rehydrated game still surfaces the name --
    the regression case where the name lived only in localStorage and
    was lost when the server's board_update payload didn't carry it.

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
PLAYER_NAME_LS_KEY = "sturddle:player_name"


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
    """Open Settings, drive the wa-input value, commit via change event, close."""
    await page.click("#settings-btn")
    await page.locator("wa-tab[panel='play']").click()
    name_input = page.locator("wa-tab-panel[name='play'] wa-input").last
    await name_input.wait_for(state="visible")
    # wa-input is a custom element; Playwright's fill() can't drive it
    # directly. Set .value on the host (the property setter forwards to
    # the internal native input) then dispatch input/change.
    await name_input.evaluate(
        f"""(el) => {{
            el.value = {name!r};
            el.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true, composed: true }}));
        }}"""
    )
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
async def test_typing_name_in_settings_persists_to_local_storage(server, page):
    """Typing the name in the Settings dialog must commit it to localStorage."""
    base = server
    await _open_app(page, base)
    await _set_name_in_settings(page, CUSTOM_NAME)

    stored = await page.evaluate(
        f"() => localStorage.getItem({PLAYER_NAME_LS_KEY!r})"
    )
    assert stored == CUSTOM_NAME, stored


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
    snapshot, since localStorage is only read at /game/new time.
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


@pytest.mark.asyncio
async def test_custom_name_in_exported_pgn(server, page):
    """The exported PGN must carry the custom name in the [White] header
    when the human plays White, and the engine's name in [Black].
    """
    base = server
    await _open_app(page, base)
    await _set_name_in_settings(page, CUSTOM_NAME)
    # Force human_white via direct POST so we don't depend on the random
    # side selection the New-game button would otherwise apply.
    httpx.post(
        f"{base}/game/new",
        json={"player_name": CUSTOM_NAME, "human_side": "white"},
    ).raise_for_status()
    httpx.post(f"{base}/game/move", json={"uci": "e2e4"}).raise_for_status()
    await _wait_for_engine_reply(page)

    pgn = httpx.get(f"{base}/game/pgn").text
    assert f'[White "{CUSTOM_NAME}"]' in pgn, pgn
    assert f'[Black "{ENGINE_NAME}"]' in pgn, pgn
