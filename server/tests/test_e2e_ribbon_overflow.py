"""E2E: vertical ribbon overflow (Playwright/Chromium).

On a desktop window too short for every ribbon button, the buttons that
don't fit move -- bottom first -- into a vertical-ellipsis button in the
ribbon's last slot. Every button is then either fully on the ribbon or in
the ellipsis menu; a menu entry clicks its real button and mirrors its
disabled state; a taller window brings the buttons back and drops the
ellipsis. "On the ribbon" means below the app header, which covers the top
of Play's and Studio's full-height rails. Checked on Play, Arena and Studio.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import (  # noqa: E402
    assert_no_page_errors,
    goto_arena_tournaments,
    run_uvicorn_subprocess,
    seed_tournament,
    TOURNAMENT_UX_KEY,
    TOURNAMENT_UX_STUDIO,
    tournament_server_env,
    wait_eval,
    wait_perspective_ready,
    watch_page_errors,
)

ENGINES = ("engine-A", "engine-B")
SHORT = {"width": 1400, "height": 400}
TALL = {"width": 1400, "height": 1000}
PLAY_RIBBON = "#board-controls"
ARENA_RIBBON = ".tournaments-ribbon"
STUDIO_RIBBON = ".studio-ribbon"
MORE_BTN = ".ribbon-more"
MORE_MENU = ".ribbon-more-menu:popover-open"

# The ribbon's own buttons (not the ellipsis): [aria-label, shown, fully
# inside the ribbon's visible part (below the header), disabled].
_BUTTONS = """(sel) => {
  const ribbon = document.querySelector(sel);
  const r = ribbon.getBoundingClientRect();
  const top = Math.max(r.top, document.querySelector('header').getBoundingClientRect().bottom);
  return [...ribbon.querySelectorAll(':scope > .ribbon-btn:not(.ribbon-more)')].map((b) => {
    const shown = getComputedStyle(b).display !== 'none';
    const x = b.getBoundingClientRect();
    return [b.getAttribute('aria-label'), shown, shown && x.top >= top && x.bottom <= r.bottom, b.disabled];
  });
}"""
_MORE_SHOWN = """(sel) => {
  const more = document.querySelector(sel).querySelector(':scope > .ribbon-more');
  return !!more && getComputedStyle(more).display !== 'none';
}"""
# Open menu's entries' icon offset from their left edge, less their left
# padding: 0 for every entry when they're left-aligned.
_MENU_ICON_INDENTS = f"""() => [...document.querySelectorAll('{MORE_MENU} .tmb-dd-item')].map((b) => {{
  const icon = b.querySelector('wa-icon').getBoundingClientRect().left;
  return Math.round(icon - b.getBoundingClientRect().left - parseFloat(getComputedStyle(b).paddingLeft));
}})"""
# Open menu's entries: [label, disabled].
_MENU_ITEMS = f"""() => [...document.querySelectorAll('{MORE_MENU} .tmb-dd-item')]
  .map((b) => [b.textContent.trim(), b.disabled])"""


@pytest.fixture
def server(tmp_path):
    seed_tournament(tmp_path, "overflow", list(ENGINES), [])
    with run_uvicorn_subprocess(env_overrides=tournament_server_env(tmp_path, ENGINES)) as base:
        yield base


async def _goto_play(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)


async def _check_overflow(page, ribbon):
    """Short window: every button fully shown or in the menu, in order, with
    its disabled state, left-aligned; at least one overflowed."""
    buttons = await page.evaluate(_BUTTONS, ribbon)
    assert all(inside for _label, shown, inside, _dis in buttons if shown), buttons
    hidden = [[label, dis] for label, shown, _inside, dis in buttons if not shown]
    assert hidden, buttons
    assert await page.evaluate(_MORE_SHOWN, ribbon)
    await page.click(f"{ribbon} > {MORE_BTN}")
    await page.wait_for_selector(MORE_MENU)
    assert await page.evaluate(_MENU_ITEMS) == hidden
    # Left-aligned: each entry's icon starts at its left padding.
    assert set(await page.evaluate(_MENU_ICON_INDENTS)) == {0}
    await page.keyboard.press("Escape")
    return hidden


async def _check_restored(page, ribbon):
    """Tall window: every button back on the ribbon, no ellipsis."""
    await page.set_viewport_size(TALL)
    await page.wait_for_function(f"(sel) => !({_MORE_SHOWN})(sel)", arg=ribbon)
    buttons = await page.evaluate(_BUTTONS, ribbon)
    assert all(shown and inside for _label, shown, inside, _dis in buttons), buttons


@pytest.mark.asyncio
async def test_play_ribbon_overflows_into_menu(server, make_page):
    _ctx, page = await make_page(viewport=SHORT)
    errors = watch_page_errors(page)
    await _goto_play(page, server)

    hidden = await _check_overflow(page, PLAY_RIBBON)
    # A menu entry clicks its real button: the last hidden one, if enabled.
    label, disabled = hidden[-1]
    if not disabled:
        await page.evaluate(
            "(l) => { window.__ribbonClicks = []; document.querySelector(`[aria-label=\"${l}\"]`)"
            ".addEventListener('click', () => window.__ribbonClicks.push(l)); }", label,
        )
        await page.click(f"{PLAY_RIBBON} > {MORE_BTN}")
        await page.locator(f"{MORE_MENU} .tmb-dd-item", has_text=label).click()
        await wait_eval(page, "() => window.__ribbonClicks", [label])

    await _check_restored(page, PLAY_RIBBON)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_arena_ribbon_overflows_into_menu(server, make_page):
    _ctx, page = await make_page(viewport=SHORT)
    errors = watch_page_errors(page)
    await goto_arena_tournaments(page, server)

    await _check_overflow(page, ARENA_RIBBON)
    await _check_restored(page, ARENA_RIBBON)
    assert_no_page_errors(errors)


@pytest.mark.asyncio
async def test_studio_ribbon_overflows_into_menu(server, make_page):
    _ctx, page = await make_page(viewport=SHORT)
    errors = watch_page_errors(page)
    await page.add_init_script(f"localStorage.setItem('{TOURNAMENT_UX_KEY}', '{TOURNAMENT_UX_STUDIO}')")
    await _goto_play(page, server)
    await page.click('button[data-perspective="engines"]')
    await page.wait_for_selector(STUDIO_RIBBON)

    await _check_overflow(page, STUDIO_RIBBON)
    await _check_restored(page, STUDIO_RIBBON)
    assert_no_page_errors(errors)
