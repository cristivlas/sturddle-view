"""Test isolation: keep tests off the user's real ~/.config files."""
from __future__ import annotations

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(scope="session")
async def browser():
    """One Chromium instance shared across the whole test session.

    Each test must call ``browser.new_context()`` for isolation; closing
    the context (not the browser) is the test's responsibility.
    Yields None when Playwright/Chromium is not installed — each e2e
    test calls pytest.skip() on None.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        yield None
        return
    async with async_playwright() as pw:
        try:
            b = await pw.chromium.launch()
        except Exception:
            yield None
            return
        yield b
        await b.close()


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, monkeypatch):
    """Redirect default user-config paths to per-test tmp locations.

    Covers the saved-game snapshot and the persisted settings file so tests
    don't read from or write into the developer's actual ~/.config dir.
    Tests that need to inspect a saved file should pass an explicit
    GameStore to create_app instead of relying on this default.
    """
    import sturddle_view.config as cfg
    import sturddle_view.play.game_store as gs
    monkeypatch.setattr(
        gs, "default_state_path", lambda: tmp_path / "current_game.json"
    )
    monkeypatch.setattr(
        cfg, "default_settings_file", lambda: tmp_path / "settings.json"
    )
