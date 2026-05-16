"""PUT /settings must validate pgn_dir at set-time.

Empty string clears the field (autosave disabled implicitly). Non-empty
values must be writable directories -- created if missing, rejected
with 400 if creation or write probe fails. Catches typos in the dialog
before autosave silently fails at game-end.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Redirect persisted settings to tmp so the test doesn't touch the
    # real user-config dir.
    monkeypatch.setattr(
        "sturddle_view.config.default_settings_file",
        lambda: tmp_path / "settings.json",
    )
    settings = Settings(token="t", auth_disabled=True)
    app = create_app(settings=settings)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer t"
        yield c


def test_pgn_dir_empty_string_clears(client, tmp_path):
    """Empty string -> pgn_dir cleared (falsy means no save)."""
    good = tmp_path / "pgns"
    good.mkdir()
    assert client.put("/settings", json={"pgn_dir": str(good)}).status_code == 200

    r = client.put("/settings", json={"pgn_dir": ""})
    assert r.status_code == 200
    assert r.json()["pgn_dir"] == ""


def test_pgn_dir_existing_writable_accepted(client, tmp_path):
    good = tmp_path / "pgns"
    good.mkdir()
    r = client.put("/settings", json={"pgn_dir": str(good)})
    assert r.status_code == 200
    assert r.json()["pgn_dir"] == str(good)


def test_pgn_dir_missing_created(client, tmp_path):
    """Non-existent path under a writable parent is auto-created."""
    target = tmp_path / "newly" / "made"
    assert not target.exists()
    r = client.put("/settings", json={"pgn_dir": str(target)})
    assert r.status_code == 200
    assert target.is_dir()


def test_pgn_dir_points_at_file_rejected(client, tmp_path):
    """A path that exists but is a regular file (not a directory) is
    rejected with 400 -- otherwise the user gets a clean dialog set
    and silent autosave failures later."""
    f = tmp_path / "not_a_dir.txt"
    f.write_text("hi")
    r = client.put("/settings", json={"pgn_dir": str(f)})
    assert r.status_code == 400
    assert "pgn_dir" in r.text.lower()


def test_pgn_dir_unwritable_path_rejected(client, tmp_path, monkeypatch):
    """If mkdir / probe-write fails, the field is rejected. Simulated by
    monkeypatching Path.mkdir to raise so the test works on every OS."""
    target = tmp_path / "would_be_bad"

    orig_mkdir = Path.mkdir

    def fake_mkdir(self, *args, **kwargs):
        if str(self) == str(target):
            raise PermissionError("simulated: cannot create")
        return orig_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    r = client.put("/settings", json={"pgn_dir": str(target)})
    assert r.status_code == 400
    assert "pgn_dir" in r.text.lower()
