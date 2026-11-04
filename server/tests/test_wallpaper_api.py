from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.api import wallpaper as wallpaper_api
from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from .conftest import REGISTRY_FILE

TOKEN = "test-token"
URL = "/wallpaper"
INFO_URL = "/wallpaper/info"
STYLE_URL = "/wallpaper/style"
PNG = "image/png"
JPEG = "image/jpeg"
PNG_BYTES = b"\x89PNG fake png"
JPEG_BYTES = b"\xff\xd8 fake jpeg"
SMALL_CAP = 8


@pytest.fixture
def client(tmp_path):
    settings = Settings(token=TOKEN)
    app = create_app(settings=settings, engine_registry=EngineRegistry(path=tmp_path / REGISTRY_FILE))
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


@pytest.fixture
def wallpaper_dir(tmp_path):
    # conftest's autouse isolation roots SV_WALLPAPER_DIR here.
    return tmp_path / "wallpaper"


def _put_image(client, data: bytes, ctype: str):
    return client.put(URL, content=data, headers={"Content-Type": ctype})


def test_info_without_wallpaper_reports_defaults(client):
    info = client.get(INFO_URL).json()
    assert info == {
        "version": None, "fade": 0, "fit": wallpaper_api.FITS[0],
        "fade_max": wallpaper_api.MAX_FADE_PCT, "fits": list(wallpaper_api.FITS),
    }
    assert client.get(URL).status_code == 404


def test_put_stores_and_serves_image(client):
    r = _put_image(client, PNG_BYTES, PNG)
    assert r.status_code == 200
    assert r.json()["version"] is not None
    got = client.get(URL)
    assert got.status_code == 200
    assert got.headers["content-type"] == PNG
    assert got.content == PNG_BYTES


def test_put_other_type_replaces_previous_file(client, wallpaper_dir):
    _put_image(client, PNG_BYTES, PNG)
    _put_image(client, JPEG_BYTES, JPEG)
    assert sorted(p.name for p in wallpaper_dir.glob("image.*")) == ["image.jpg"]
    got = client.get(URL)
    assert got.headers["content-type"] == JPEG
    assert got.content == JPEG_BYTES


@pytest.mark.parametrize("data, ctype", [
    (PNG_BYTES, "image/gif"),
    (PNG_BYTES, "text/plain"),
    (b"", PNG),
])
def test_put_rejects_bad_upload(client, data, ctype):
    assert _put_image(client, data, ctype).status_code == 400
    assert client.get(INFO_URL).json()["version"] is None


def test_put_rejects_oversized_declared_length(client, monkeypatch):
    monkeypatch.setattr(wallpaper_api, "MAX_WALLPAPER_BYTES", SMALL_CAP)
    assert _put_image(client, b"x" * (SMALL_CAP + 1), PNG).status_code == 400
    assert client.get(INFO_URL).json()["version"] is None


def test_put_rejects_oversized_stream_without_length(client, monkeypatch):
    monkeypatch.setattr(wallpaper_api, "MAX_WALLPAPER_BYTES", SMALL_CAP)
    # A generator body goes out chunked: no Content-Length to check up front.
    chunks = (b"x" * SMALL_CAP for _ in range(2))
    r = client.put(URL, content=chunks, headers={"Content-Type": PNG})
    assert r.status_code == 400
    assert client.get(INFO_URL).json()["version"] is None


def test_put_at_cap_is_accepted(client, monkeypatch):
    monkeypatch.setattr(wallpaper_api, "MAX_WALLPAPER_BYTES", SMALL_CAP)
    assert _put_image(client, b"x" * SMALL_CAP, PNG).status_code == 200


def test_delete_removes_image(client):
    _put_image(client, PNG_BYTES, PNG)
    r = client.delete(URL)
    assert r.status_code == 200
    assert r.json()["version"] is None
    assert client.get(URL).status_code == 404


def test_style_merges_and_survives_image_changes(client):
    client.put(STYLE_URL, json={"fade": 40})
    client.put(STYLE_URL, json={"fit": "tile"})
    _put_image(client, PNG_BYTES, PNG)
    client.delete(URL)
    info = client.get(INFO_URL).json()
    assert (info["fade"], info["fit"]) == (40, "tile")


@pytest.mark.parametrize("payload", [
    {"fade": -1},
    {"fade": wallpaper_api.MAX_FADE_PCT + 1},
    {"fade": 12.5},
    {"fade": True},
    {"fit": "zoom"},
    {"opacity": 10},
])
def test_style_rejects_bad_values(client, payload):
    assert client.put(STYLE_URL, json=payload).status_code == 400
    info = client.get(INFO_URL).json()
    assert (info["fade"], info["fit"]) == (0, wallpaper_api.FITS[0])


def test_unreadable_style_file_reads_as_defaults(client, wallpaper_dir):
    wallpaper_dir.mkdir(parents=True, exist_ok=True)
    (wallpaper_dir / "style.json").write_text("{not json", encoding="utf-8")
    info = client.get(INFO_URL).json()
    assert (info["fade"], info["fit"]) == (0, wallpaper_api.FITS[0])


def test_endpoints_require_token(client):
    del client.headers["Authorization"]
    assert client.get(INFO_URL).status_code == 401
    assert _put_image(client, PNG_BYTES, PNG).status_code == 401
