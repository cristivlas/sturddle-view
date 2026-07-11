"""E2E: the opening-book inheritance chain and its override combos.

Hop 1 -- Settings > Tournament inherits the Common book with a tri-state
(inherit / explicit no-book / own book) that must persist across dialog
reopens (X once broke persistence: no input/change event reached the
autosave listeners).

Hop 2 -- the New Tournament dialog inherits the settings chain
(default_template first, Common beneath) and can override locally: its own
book, no book, or a depth override on top of an inherited path. Created
tournaments freeze the resolved values into engine_defaults.

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

COMMON_BOOK = "/common/opening.epd"
COMMON_PLIES = 8
INHERIT_COMMON = "inherits: opening.epd"
NO_BOOK = "(no book)"
BASE_TPL = {"tc": "10+0.1", "rounds": 2, "games_in_parallel": 1}


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


def _env(tmp_path, settings=None):
    registry_path = tmp_path / "engines.json"
    seed = EngineRegistry(path=registry_path)
    for n in ("alpha", "beta"):
        seed.add(name=n, path=_make_fake_uci(tmp_path, n))
    if settings is None:
        settings = {
            "engine_default_book_path": COMMON_BOOK,
            "engine_default_book_plies": COMMON_PLIES,
        }
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return {
        "SV_PGN_DIR": str(tmp_path / "pgn"),
        "SV_TOURNAMENT_ROOT": str(tmp_path / "tournaments"),
        "SV_TOURNAMENT_FASTCHESS_PATH": sys.executable,
        "SV_ENGINE_REGISTRY_PATH": str(registry_path),
        "SV_IMPORTS_DIR": str(tmp_path / "imports"),
        "SV_SETTINGS_FILE": str(tmp_path / "settings.json"),
        "SV_GAME_STATE_PATH": str(tmp_path / "current_game.json"),
    }


async def _goto_app(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)


# ---- Settings tab driving -------------------------------------------------

SETTINGS_BOOK = ".settings-tournament-tpl-mount .ttf-book"


async def _open_settings_book(page):
    await page.evaluate(
        """() => window.dispatchEvent(new CustomEvent(
            'sturddle:open-settings', { detail: { tab: 'tournament' } }))""")
    await page.locator(f"{SETTINGS_BOOK} .path-field").first.wait_for(state="attached")


async def _settings_book_field(page):
    return await page.evaluate(
        f"""() => {{ const f = document.querySelector('{SETTINGS_BOOK} .path-field');
            return {{ value: f.value || "", placeholder: f.placeholder }}; }}""")


async def _click_book_x(page, scope):
    """Click the book row's X (second action button)."""
    await page.evaluate(
        f"""() => document.querySelectorAll(
            '{scope} .settings-row-actions wa-button')[1].click()""")


async def _type_book_path(page, scope, path):
    await page.evaluate(
        f"""(p) => {{
            const f = document.querySelector('{scope} .path-field');
            f.value = p;
            f.dispatchEvent(new Event('input', {{ bubbles: true }}));
            f.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}""", path)


def _is_settings_put(r):
    return r.url.endswith("/api/tournament-settings") and r.request.method == "PUT"


async def _get_default_template(page):
    return await page.evaluate(
        "() => fetch('/api/tournament-settings').then(r => r.json())"
        ".then(s => s.default_template || {})")


async def _close_settings(page):
    await page.keyboard.press("Escape")
    await page.wait_for_function("() => !document.querySelector('wa-dialog[open]')")


@pytest.mark.asyncio
async def test_settings_book_tri_state_persists(tmp_path, make_page):
    env = _env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        await _goto_app(page, base)

        # Fresh state inherits the Common book.
        await _open_settings_book(page)
        assert await _settings_book_field(page) == {"value": "", "placeholder": INHERIT_COMMON}

        # X -> explicit no-book; must persist across reopen (regression: the
        # X click fired no input/change event, so autosave never ran).
        async with page.expect_response(_is_settings_put):
            await _click_book_x(page, SETTINGS_BOOK)
        assert (await _settings_book_field(page))["placeholder"] == NO_BOOK
        assert (await _get_default_template(page)).get("book_path") == ""
        await _close_settings(page)
        await _open_settings_book(page)
        assert (await _settings_book_field(page))["placeholder"] == NO_BOOK

        # Own book overrides.
        async with page.expect_response(_is_settings_put):
            await _type_book_path(page, SETTINGS_BOOK, "/mine/book.epd")
        assert (await _get_default_template(page)).get("book_path") == "/mine/book.epd"
        await _close_settings(page)
        await _open_settings_book(page)
        assert (await _settings_book_field(page))["value"] == "/mine/book.epd"

        # X -> no-book, X again -> back to inherit (book keys dropped). The
        # debounced autosave coalesces both clicks into one PUT.
        async with page.expect_response(_is_settings_put):
            await _click_book_x(page, SETTINGS_BOOK)
            await _click_book_x(page, SETTINGS_BOOK)
        assert (await _settings_book_field(page))["placeholder"] == INHERIT_COMMON
        assert "book_path" not in (await _get_default_template(page))
        await _close_settings(page)
        await _open_settings_book(page)
        assert (await _settings_book_field(page))["placeholder"] == INHERIT_COMMON


@pytest.mark.asyncio
async def test_settings_unrelated_edit_keeps_book_unset(tmp_path, make_page):
    """No book anywhere: editing an unrelated template field must not bake an
    explicit book_path:"" into default_template -- that would silently
    suppress a Common book configured later."""
    env = _env(tmp_path, settings={})
    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        await _goto_app(page, base)
        await _open_settings_book(page)
        assert (await _settings_book_field(page))["placeholder"] == NO_BOOK

        # Edit Rounds -> autosave PUTs the template; book keys must stay absent.
        async with page.expect_response(_is_settings_put):
            await page.evaluate(
                """() => {
                    const r = document.querySelector(
                        '.settings-tournament-tpl-mount wa-input[data-key="rounds"]');
                    r.value = '7';
                    r.dispatchEvent(new Event('input', { bubbles: true }));
                }""")
        tpl = await _get_default_template(page)
        assert tpl.get("rounds") == 7
        assert "book_path" not in tpl, f"unrelated edit baked book_path: {tpl}"


# ---- New Tournament dialog driving ------------------------------------------

DIALOG_BOOK = ".new-tournament-form .ttf-book"


async def _put_default_template(page, tpl):
    await page.evaluate(
        """async (tpl) => {
            await fetch('/api/tournament-settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ default_template: tpl }),
            });
        }""", tpl)


async def _open_new_dialog(page):
    await page.wait_for_function(
        "() => document.querySelector('.t-new') && !document.querySelector('.t-new').disabled")
    await page.click(".t-new")
    await page.locator(".ne-available-list .ne-item").first.wait_for(state="attached")
    await page.locator(".ne-available-list .ne-item", has_text="alpha").click()
    await page.locator(".ne-available-list .ne-item", has_text="beta").click(modifiers=["Control"])
    await page.locator(".ne-add").click()
    await page.locator(f"{DIALOG_BOOK} .path-field").first.wait_for(state="attached")


async def _dialog_book_field(page):
    return await page.evaluate(
        f"""() => {{ const f = document.querySelector('{DIALOG_BOOK} .path-field');
            return {{ value: f.value || "", placeholder: f.placeholder }}; }}""")


async def _set_dialog_plies(page, plies):
    await page.evaluate(
        f"""(v) => {{
            const p = document.querySelector('{DIALOG_BOOK} .ttf-book-opts wa-input');
            p.value = String(v);
            p.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }}""", plies)


async def _create(page, name):
    await page.evaluate(
        """(name) => {
            const n = document.querySelector('.nt-name');
            n.value = name;
            n.dispatchEvent(new Event('input', { bubbles: true }));
        }""", name)
    await page.wait_for_function(
        """() => { const b = [...document.querySelectorAll('wa-dialog wa-button[slot="footer"]')]
            .find(el => el.textContent.trim() === 'Create'); return b && !b.disabled; }""")
    await page.evaluate(
        """() => [...document.querySelectorAll('wa-dialog wa-button[slot="footer"]')]
            .find(el => el.textContent.trim() === 'Create').click()""")
    await page.wait_for_function("() => !document.querySelector('wa-dialog[open]')")


async def _frozen_book(page, name):
    return await page.evaluate(
        """async (name) => {
            const l = await fetch('/api/tournaments').then(r => r.json());
            const t = (l.tournaments || []).find(x => x.name === name);
            if (!t) return null;
            const ed = t.engine_defaults || {};
            return { path: ed.book_path, plies: ed.book_plies };
        }""", name)


@pytest.mark.asyncio
async def test_new_dialog_book_chain_combos(tmp_path, make_page):
    env = _env(tmp_path)
    with run_uvicorn_subprocess(env_overrides=env) as base:
        _ctx, page = await make_page(viewport={"width": 1400, "height": 900})
        await pin_arena_tournament_ux(page)
        await _goto_app(page, base)
        await page.click('button[data-perspective="engines"]')
        await page.wait_for_selector(".tournaments-panel")

        # a) default_template inherit -> dialog inherits Common; freeze Common.
        await _put_default_template(page, BASE_TPL)
        await _open_new_dialog(page)
        assert (await _dialog_book_field(page))["placeholder"] == INHERIT_COMMON
        await _create(page, "t-inherit")
        assert await _frozen_book(page, "t-inherit") == {"path": COMMON_BOOK, "plies": COMMON_PLIES}

        # b) default_template explicit no-book -> dialog off; freeze no book.
        await _put_default_template(page, {**BASE_TPL, "book_path": ""})
        await _open_new_dialog(page)
        assert (await _dialog_book_field(page))["placeholder"] == NO_BOOK
        await _create(page, "t-off")
        assert await _frozen_book(page, "t-off") == {"path": None, "plies": None}

        # c) default_template own book -> dialog inherits it over Common.
        await _put_default_template(page, {**BASE_TPL, "book_path": "/tpl/own.epd", "book_plies": 4})
        await _open_new_dialog(page)
        assert (await _dialog_book_field(page))["placeholder"] == "inherits: own.epd"
        await _create(page, "t-tpl")
        assert await _frozen_book(page, "t-tpl") == {"path": "/tpl/own.epd", "plies": 4}

        # d) dialog-local X -> no book despite inherited chain.
        await _put_default_template(page, BASE_TPL)
        await _open_new_dialog(page)
        await _click_book_x(page, DIALOG_BOOK)
        assert (await _dialog_book_field(page))["placeholder"] == NO_BOOK
        await _create(page, "t-xoff")
        assert await _frozen_book(page, "t-xoff") == {"path": None, "plies": None}

        # e) dialog-local own book.
        await _put_default_template(page, BASE_TPL)
        await _open_new_dialog(page)
        await _type_book_path(page, DIALOG_BOOK, "/dlg/mine.pgn")
        await _create(page, "t-dlgset")
        frozen = await _frozen_book(page, "t-dlgset")
        assert frozen["path"] == "/dlg/mine.pgn"

        # f) depth override on top of an inherited path.
        await _put_default_template(page, BASE_TPL)
        await _open_new_dialog(page)
        await _set_dialog_plies(page, 12)
        await _create(page, "t-plies")
        assert await _frozen_book(page, "t-plies") == {"path": COMMON_BOOK, "plies": 12}
