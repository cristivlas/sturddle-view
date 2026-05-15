"""Test isolation: keep tests off the user's real ~/.config files."""
from __future__ import annotations

import pytest
import pytest_asyncio


def pytest_addoption(parser):
    parser.addoption("--syzygy-path", default=None, help="Path to Syzygy tablebase files")


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

    Covers every default that would otherwise resolve to the developer's
    real platformdirs/~/.config tree: the saved-game snapshot, persisted
    settings, the engine registry, and the tournament store root. Without
    this, a test that constructs ``create_app(settings=...)`` without
    overriding ``tournament_root`` would mount the user's *live*
    tournament directory and the orchestrator's startup reconcile would
    flip any in-flight tournament to ``failed``.

    Tests that need to inspect a saved file should pass explicit paths
    (e.g. ``GameStore``, ``EngineRegistry(path=...)``, or
    ``Settings.tournament_root``) instead of relying on these defaults.
    """
    import sturddle_view.app as app_mod
    import sturddle_view.config as cfg
    import sturddle_view.engines as engines_mod
    import sturddle_view.play.game_store as gs
    import sturddle_view.recent_imports as ri_mod
    import sturddle_view.tournament.store as ts_mod
    monkeypatch.setattr(
        gs, "default_state_path", lambda: tmp_path / "current_game.json"
    )
    monkeypatch.setattr(
        cfg, "default_settings_file", lambda: tmp_path / "settings.json"
    )
    monkeypatch.setattr(
        engines_mod, "default_registry_path", lambda: tmp_path / "engines.json"
    )
    monkeypatch.setattr(
        ri_mod, "default_imports_dir", lambda: tmp_path / "imports"
    )
    # Patch both the canonical symbol and app.py's local import binding.
    fake_root = tmp_path / "tournaments"
    monkeypatch.setattr(ts_mod, "default_root", lambda: fake_root)
    monkeypatch.setattr(app_mod, "default_root", lambda: fake_root)
