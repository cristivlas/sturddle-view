"""Test isolation: keep tests off the user's real ~/.config files."""
from __future__ import annotations

import pytest


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
