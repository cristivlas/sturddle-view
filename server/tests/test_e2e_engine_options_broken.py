"""E2E: opening Engine Settings for a non-launchable engine offers to remove it.

Regression: previously the dialog opened with a torn-up layout (the long
"UCI probe failed: ..." note collided with the Name input in the 2-column
grid). The user couldn't meaningfully edit anything anyway, so the click
on Edit now confirms removal instead.

Skipped if Playwright / Chromium isn't available.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402


BROKEN_NAME = "BrokenEngine"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    import uvicorn

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    # Seed a pre-existing broken engine: file exists but is not a runnable
    # program -- mirrors a registry entry left behind from before the
    # "reject unprobeable on add" fix.
    broken_path = tmp_path / "not-an-engine.txt"
    broken_path.write_text("this is not a UCI engine\n")
    registry.add(name=BROKEN_NAME, path=str(broken_path))
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


@pytest.mark.asyncio
async def test_edit_unlaunchable_engine_offers_removal(server, browser):
    """Clicking Edit on a broken engine opens a Remove? confirm, not the Options form."""
    if browser is None:
        pytest.skip("chromium not installed")

    ctx = await browser.new_context(viewport={"width": 1200, "height": 800})
    page = await ctx.new_page()
    try:
        await page.goto(server + "/")
        await page.wait_for_selector("#play-perspective", timeout=5000)

        # Open Settings dialog directly to the Engines tab.
        await page.evaluate(
            "window.dispatchEvent(new CustomEvent('sturddle:open-settings', "
            "{ detail: { tab: 'engines' } }))"
        )
        # Wait for the engines list row to render.
        row = page.locator(".engines-list-item", has_text=BROKEN_NAME)
        await row.wait_for(timeout=3000)
        await row.click()

        # Click Edit (engine settings) -- this triggers auto-refresh-schema
        # against the broken executable, which fails, which should now
        # prompt the user to remove the engine.
        await page.locator(".engines-detail-options").click()

        # The confirm dialog text contains the engine name and "cannot be launched".
        confirm_dialog = page.locator("wa-dialog", has_text="cannot be launched")
        await confirm_dialog.wait_for(state="attached", timeout=3000)
        body_text = await confirm_dialog.inner_text()
        assert BROKEN_NAME in body_text
        # The Engine Settings dialog must NOT be open in its place.
        assert "Refresh" not in body_text, (
            "Engine Options dialog appeared instead of the Remove? confirm"
        )

        # Confirm removal.
        await confirm_dialog.get_by_role("button", name="Remove").click()

        # Engine row should be gone from the list.
        await page.locator(".engines-list-item", has_text=BROKEN_NAME).wait_for(
            state="detached", timeout=3000,
        )
    finally:
        await ctx.close()
