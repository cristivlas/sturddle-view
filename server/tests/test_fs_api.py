from __future__ import annotations

import stat
import sys

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry


@pytest.fixture
def client(tmp_path):
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer test-token"
        yield c


def test_list_dir(client, tmp_path):
    (tmp_path / "subdir").mkdir()
    (tmp_path / "file.txt").write_text("hi")
    r = client.get(f"/fs?path={tmp_path}")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(tmp_path)
    names = [e["name"] for e in body["entries"]]
    assert names == ["subdir", "file.txt"]  # dirs first, then files


def test_list_hides_dotfiles_by_default(client, tmp_path):
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / "visible").write_text("x")
    r = client.get(f"/fs?path={tmp_path}")
    names = [e["name"] for e in r.json()["entries"]]
    assert "visible" in names
    assert ".hidden" not in names


def test_list_show_hidden(client, tmp_path):
    (tmp_path / ".hidden").write_text("x")
    r = client.get(f"/fs?path={tmp_path}&show_hidden=true")
    names = [e["name"] for e in r.json()["entries"]]
    assert ".hidden" in names


def test_list_404_on_missing(client, tmp_path):
    r = client.get(f"/fs?path={tmp_path}/does-not-exist")
    assert r.status_code == 404


def test_list_400_on_file(client, tmp_path):
    p = tmp_path / "f"
    p.write_text("x")
    r = client.get(f"/fs?path={p}")
    assert r.status_code == 400


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX exec bit only")
def test_stat_executable_flag(client, tmp_path):
    p = tmp_path / "bin"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    r = client.get(f"/fs/stat?path={p}")
    assert r.status_code == 200
    body = r.json()
    assert body["is_file"]
    assert body["is_executable"]


def test_stat_404(client, tmp_path):
    r = client.get(f"/fs/stat?path={tmp_path}/nope")
    assert r.status_code == 404


def test_auth_required(tmp_path):
    settings = Settings(token="test-token")
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        assert c.get(f"/fs?path={tmp_path}").status_code == 401
