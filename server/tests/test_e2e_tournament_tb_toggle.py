"""E2E: Tablebase-adjudication toggle gating + adjudication prefill round-trip.

The Tablebase toggle in the template form is enabled only when a global
SyzygyPath is configured (it has no meaning otherwise -- fastchess needs the
path for ``-tb``). Both form callers must wire the path through:
Settings -> Tournament (default-template editor) and the New Tournament dialog.
This was missed once because no test rendered the Settings tab.

Also round-trips the two adjudication sub-toggles (Two-sided, Tablebase) that
the prefill suite doesn't cover: edit the default template, reopen New, assert
the switches reflect the saved state.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    pin_arena_tournament_ux,
    run_uvicorn_subprocess,
    wait_perspective_ready,
)

SYZYGY_DIR = "C:/tb/3-4-5" if sys.platform.startswith("win") else "/tb/3-4-5"


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


def _make_server(tmp_path, settings_extra: dict):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    for n in ("alpha", "beta"):
        seed.add(name=n, path=_make_fake_uci(tmp_path, n))
    (tmp_path / "settings.json").write_text(
        json.dumps(settings_extra), encoding="utf-8",
    )
    env = {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
        "SV_ENGINE_REGISTRY_PATH": str(registry_path),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }
    return env


def _watch_errors(page):
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return errors


async def _open_settings_tournament(page):
    """Deep-link the Settings dialog to its Tournament tab and wait for the
    template form to render there."""
    await page.evaluate(
        """() => window.dispatchEvent(new CustomEvent(
            'sturddle:open-settings', { detail: { tab: 'tournament' } }))"""
    )
    await page.locator(
        ".settings-tournament-tpl-mount wa-switch[data-key='tb_adjudication']"
    ).first.wait_for(state="attached")


async def _tb_disabled_in_settings(page):
    return await page.evaluate(
        """() => document.querySelector(
            ".settings-tournament-tpl-mount wa-switch[data-key='tb_adjudication']"
        )?.hasAttribute('disabled')"""
    )


@pytest.mark.asyncio
async def test_settings_tb_toggle_enabled_when_syzygy_set(tmp_path, make_page):
    """SyzygyPath configured -> the Settings template-form Tablebase toggle is
    enabled. Guards the wiring the original change missed."""
    env = _make_server(tmp_path, {"engine_default_syzygy_path": SYZYGY_DIR})
    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = _watch_errors(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)
        await _open_settings_tournament(page)
        assert (await _tb_disabled_in_settings(page)) is False, \
            "Tablebase toggle should be enabled when SyzygyPath is set"
        assert errors == [], "JS errors:\n" + "\n".join(errors)


@pytest.mark.asyncio
async def test_settings_tb_toggle_disabled_when_no_syzygy(tmp_path, make_page):
    """No SyzygyPath -> the toggle is disabled (the -tb flag would be meaningless)."""
    env = _make_server(tmp_path, {})
    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = _watch_errors(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)
        await _open_settings_tournament(page)
        assert (await _tb_disabled_in_settings(page)) is True, \
            "Tablebase toggle should be disabled when SyzygyPath is unset"
        assert errors == [], "JS errors:\n" + "\n".join(errors)


async def _adj_switch_state(page):
    """Read checked-state of the New dialog's Two-sided + Tablebase switches."""
    await page.locator(
        ".nt-template-host wa-switch[data-key='tb_adjudication']"
    ).first.wait_for(state="attached")
    return await page.evaluate(
        """() => {
            const get = (k) => document.querySelector(
                `.nt-template-host wa-switch[data-key="${k}"]`
            )?.hasAttribute('checked');
            return {
                twosided: get('resign.twosided'),
                tb_adjudication: get('tb_adjudication'),
            };
        }"""
    )


@pytest.mark.asyncio
async def test_new_dialog_prefills_adjudication_subtoggles(tmp_path, make_page):
    """A default template with resign.twosided + tb_adjudication set (and
    SyzygyPath configured) round-trips into the New Tournament dialog's
    switches."""
    env = _make_server(tmp_path, {
        "engine_default_syzygy_path": SYZYGY_DIR,
        "tournament_default_template": {
            "tc": "10+0.1", "rounds": 4, "games_in_parallel": 1,
            "resign": {"movecount": 3, "score": 700, "twosided": True},
            "tb_adjudication": True,
        },
    })
    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        errors = _watch_errors(page)
        await pin_arena_tournament_ux(page)
        await page.goto(base + "/")
        await page.wait_for_selector("#play-perspective")
        await wait_perspective_ready(page)
        await page.click('button[data-perspective="engines"]')
        await page.wait_for_selector(".tournaments-panel")
        await page.wait_for_function(
            "() => document.querySelector('.t-new') && !document.querySelector('.t-new').disabled",
        )
        await page.click(".t-new")
        state = await _adj_switch_state(page)
        assert state == {"twosided": True, "tb_adjudication": True}, \
            f"adjudication sub-toggles not prefilled: {state}"
        assert errors == [], "JS errors:\n" + "\n".join(errors)
