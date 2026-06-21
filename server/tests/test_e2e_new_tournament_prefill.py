"""E2E: New Tournament dialog prefills from the saved default template.

The dialog is shared by both views, but it must reflect a default_template
edited in Settings *in the same session*. Arena refreshes its settings cache
on the SETTINGS_CHANGED event; Studio caches tournament-settings once at mount
and never refreshes -- so its New dialog shows stale defaults. Both views are
driven here so the fix can't regress one while fixing the other.

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

UX_LS_KEY = "sturddle:tournament:ux"
# Edited via the Settings dialog mid-session; values differ from the
# hardcoded dialog fallback so a stale read is unambiguous.
EDITED = {"tc": "60+0.6", "rounds": "42", "games_in_parallel": "4"}


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
    for n in ("alpha", "beta"):
        seed.add(name=n, path=_make_fake_uci(tmp_path, n))
    # Seed a non-default template on disk so the *first* dialog open (no
    # same-session edit) has something distinctive to prefill from.
    (tmp_path / "settings.json").write_text(
        json.dumps({"tournament_default_template": {
            "tc": "12+0.12", "rounds": 7, "games_in_parallel": 2,
        }}),
        encoding="utf-8",
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
    with run_uvicorn_subprocess(env_overrides=env) as base:
        yield base


def _watch_errors(page):
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    return errors


async def _edit_default_template(page):
    """Change the default_template via the same contract the Settings dialog
    uses: PUT /api/tournament-settings + a SETTINGS_CHANGED event. (Synthetic
    wa-input edits don't commit reliably for the form's debounced getValues
    read, so the contract is modeled directly.) Waits for the refresh to
    settle so a view-side cache has had its chance to update."""
    await page.evaluate(
        """async (edited) => {
            await fetch('/api/tournament-settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ default_template: {
                    tc: edited.tc,
                    rounds: Number(edited.rounds),
                    games_in_parallel: Number(edited.games_in_parallel),
                } }),
            });
            window.dispatchEvent(new CustomEvent('sturddle:settings-changed'));
        }""",
        arg=EDITED,
    )
    await page.wait_for_function(
        """(edited) => fetch('/api/tournament-settings').then(r => r.json()).then(s => {
            const tpl = s.default_template || {};
            return tpl.tc === edited.tc
                && String(tpl.rounds) === edited.rounds
                && String(tpl.games_in_parallel) === edited.games_in_parallel;
        })""",
        arg=EDITED,
    )


async def _template_values(page):
    """Read the New dialog template form's current field values, waiting until
    the form has rendered (tc input present)."""
    await page.locator(".nt-template-host wa-input[data-key='tc']").first.wait_for(
        state="attached",
    )
    return await page.evaluate(
        """() => {
            const get = (k) => document.querySelector(
                `.nt-template-host wa-input[data-key="${k}"]`
            )?.value;
            return { tc: get('tc'), rounds: get('rounds'), games_in_parallel: get('games_in_parallel') };
        }"""
    )


async def _open_engines_perspective(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.click('button[data-perspective="engines"]')


async def _open_new_arena(page):
    await page.wait_for_function(
        "() => document.querySelector('.t-new') && !document.querySelector('.t-new').disabled",
    )
    await page.click(".t-new")


async def _open_new_studio(page):
    await page.click(".studio-new")


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["arena", "studio"])
async def test_new_dialog_prefills_seeded_defaults(view, server, make_page):
    """First open (no same-session edit): both views prefill from the
    disk-seeded default_template. Guards the form-read path itself."""
    seeded = {"tc": "12+0.12", "rounds": "7", "games_in_parallel": "2"}
    _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
    errors = _watch_errors(page)
    if view == "arena":
        await pin_arena_tournament_ux(page)
    else:
        await page.add_init_script(f"localStorage.setItem('{UX_LS_KEY}', 'studio')")
    await _open_engines_perspective(page, server)
    if view == "arena":
        await page.wait_for_selector(".tournaments-panel")
        await _open_new_arena(page)
    else:
        await page.wait_for_selector(".studio-panel")
        await _open_new_studio(page)
    vals = await _template_values(page)
    assert vals == seeded, f"{view} New dialog not prefilled from seeded defaults: {vals}"
    assert errors == [], "JS errors:\n" + "\n".join(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["arena", "studio"])
async def test_new_dialog_prefills_after_same_session_edit(view, server, make_page):
    """Edit the defaults in Settings, then open New in the same session: the
    dialog must reflect the edit. Studio's mount-time cache makes this fail
    until the dialog reads the settings fresh."""
    _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
    errors = _watch_errors(page)
    if view == "arena":
        await pin_arena_tournament_ux(page)
    else:
        await page.add_init_script(f"localStorage.setItem('{UX_LS_KEY}', 'studio')")
    await _open_engines_perspective(page, server)
    await page.wait_for_selector(".tournaments-panel" if view == "arena" else ".studio-panel")

    await _edit_default_template(page)

    if view == "arena":
        await _open_new_arena(page)
    else:
        await _open_new_studio(page)
    vals = await _template_values(page)
    assert vals == EDITED, f"{view} New dialog not prefilled from edited defaults: {vals}"
    assert errors == [], "JS errors:\n" + "\n".join(errors)
