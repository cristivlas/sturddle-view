"""E2E: a client signed in with the remote token (the Connect-from-mobile
QR) doesn't see the controls the server would refuse it; the host token
still sees them.

Skipped if Playwright/Chromium isn't installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    REGISTRY_FILE,
    e2e_env,
    make_fake_uci,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

HOST_TOKEN = "host-token"
REMOTE_TOKEN = "remote-token"
VIEWPORT = {"width": 1280, "height": 800}
TOURNAMENTS_TAB = 'nav#perspective-nav button[data-perspective="engines"]'
APPEARANCE_BTN = ".wallpaper-btn"
# paint() has run once the wallpaper info is in: the button is enabled.
APPEARANCE_PAINTED = "() => document.querySelector('.wallpaper-btn')?.disabled === false"
HOST_ONLY_RIBBON = ["New tournament", "Edit tournament", "Copy tournament", "Delete tournament"]
ALWAYS_RIBBON = ["Start tournament", "Stop tournament"]


def _server_env(tmp_path):
    registry = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    registry.add(name="engine-A", path=make_fake_uci(tmp_path, "engine-A"))
    return {
        **e2e_env(tmp_path),
        "SV_AUTH_DISABLED": "0",
        "SV_TOKEN": HOST_TOKEN,
        "SV_REMOTE_TOKEN": REMOTE_TOKEN,
    }


async def _visible(page, selector: str) -> bool:
    return await page.locator(selector).first.is_visible()


async def _ribbon_visible(page, label: str) -> bool:
    return await _visible(page, f'button.ribbon-btn[aria-label="{label}"]')


@pytest.mark.asyncio
@pytest.mark.parametrize(("token", "remote"), [(REMOTE_TOKEN, True), (HOST_TOKEN, False)])
async def test_host_only_controls_follow_the_token(tmp_path, make_page, token, remote):
    with run_uvicorn_subprocess(env_overrides=_server_env(tmp_path)) as base:
        _ctx, page = await make_page(viewport=VIEWPORT)
        errors = watch_page_errors(page)
        await page.goto(f"{base}/auth?token={token}")
        await wait_perspective_ready(page)
        assert await _visible(page, "#settings-btn") is not remote
        await page.wait_for_function(APPEARANCE_PAINTED)
        assert await _visible(page, APPEARANCE_BTN) is not remote

        await page.click(TOURNAMENTS_TAB)
        await wait_perspective_ready(page)
        for label in HOST_ONLY_RIBBON:
            assert await _ribbon_visible(page, label) is not remote, label
        for label in ALWAYS_RIBBON:
            assert await _ribbon_visible(page, label), label
        assert not errors
