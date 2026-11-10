"""Token scopes: the host token can do anything; the remote token (the
Connect-from-mobile QR) can play and watch but not change what the server
runs or where it writes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sturddle_view import netinfo
from sturddle_view.app import create_app
from sturddle_view.auth import AUTH_COOKIE
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from .conftest import REGISTRY_FILE

HOST_TOKEN = "host-token"
REMOTE_TOKEN = "remote-token"
HOST = "192.168.1.5"
PORT = 8765

REMOTE_ALLOWED = [
    ("GET", "/settings"),
    ("GET", "/engines"),
    ("GET", "/api/tournaments"),
    ("GET", "/api/tournament-settings"),
    ("GET", "/wallpaper/info"),
    ("GET", "/openings"),
]
HOST_ONLY = [
    ("PUT", "/settings"),
    ("POST", "/settings/reset"),
    ("POST", "/engines"),
    ("POST", "/engines/probe"),
    ("GET", "/fs"),
    ("POST", "/connect/lan"),
    ("POST", "/api/tournaments"),
    ("PUT", "/api/tournament-settings"),
    ("DELETE", "/wallpaper"),
    ("DELETE", "/game/recent-imports/abc"),
]


@pytest.fixture
def client(tmp_path):
    settings = Settings(token=HOST_TOKEN, remote_token=REMOTE_TOKEN)
    registry = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(("method", "path"), REMOTE_ALLOWED)
def test_remote_may_play_and_watch(client, method, path):
    assert client.request(method, path, headers=_bearer(REMOTE_TOKEN)).status_code == 200


@pytest.mark.parametrize(("method", "path"), HOST_ONLY)
def test_remote_is_refused_host_routes(client, method, path):
    assert client.request(method, path, headers=_bearer(REMOTE_TOKEN), json={}).status_code == 403


@pytest.mark.parametrize(("method", "path"), HOST_ONLY)
def test_host_passes_the_scope_check(client, method, path):
    resp = client.request(method, path, headers=_bearer(HOST_TOKEN), json={})
    assert resp.status_code not in (401, 403)


@pytest.mark.parametrize(("method", "path"), REMOTE_ALLOWED + HOST_ONLY)
def test_unknown_token_is_unauthorized(client, method, path):
    assert client.request(method, path, headers=_bearer("nope"), json={}).status_code == 401


@pytest.mark.parametrize("token", [HOST_TOKEN, REMOTE_TOKEN])
def test_handshake_sets_cookie_to_the_presented_token(client, token):
    resp = client.get("/auth", params={"token": token}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.cookies[AUTH_COOKIE] == token


def test_handshake_rejects_unknown_token(client):
    assert client.get("/auth", params={"token": "nope"}, follow_redirects=False).status_code == 401


def test_remote_cookie_is_scoped_like_the_token(client):
    client.get("/auth", params={"token": REMOTE_TOKEN}, follow_redirects=False)
    assert client.get("/settings").status_code == 200
    assert client.get("/fs").status_code == 403


def test_remote_may_open_the_event_stream(client):
    with client.websocket_connect("/ws", headers=_bearer(REMOTE_TOKEN)):
        pass


def test_event_stream_refuses_unknown_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=_bearer("nope")) as ws:
            ws.receive_json()


def test_entry_url_remote_carries_remote_token():
    s = Settings(token=HOST_TOKEN, remote_token=REMOTE_TOKEN, port=PORT)
    base = f"http://{HOST}:{PORT}/auth?token="
    assert netinfo.entry_url(s, HOST, remote=True) == base + REMOTE_TOKEN
    assert netinfo.entry_url(s, HOST) == base + HOST_TOKEN


@pytest.mark.parametrize(("token", "role"), [(HOST_TOKEN, "host"), (REMOTE_TOKEN, "remote")])
def test_role_endpoint_names_the_scope(client, token, role):
    assert client.get("/auth/role", headers=_bearer(token)).json() == {"role": role}


def test_role_endpoint_needs_a_token(client):
    assert client.get("/auth/role").status_code == 401


def test_remote_may_run_the_fastchess_check_before_a_start(client):
    resp = client.get("/api/tournament-settings/fastchess-check", headers=_bearer(REMOTE_TOKEN))
    assert resp.status_code not in (401, 403)


@pytest.mark.parametrize("bad", ["\u00e9t\u00e9", "\U0001f600"])
def test_non_ascii_token_is_unauthorized(client, bad):
    assert client.get("/auth", params={"token": bad}, follow_redirects=False).status_code == 401


def test_remote_link_keeps_an_existing_host_session(client):
    client.get("/auth", params={"token": HOST_TOKEN}, follow_redirects=False)
    resp = client.get("/auth", params={"token": REMOTE_TOKEN}, follow_redirects=False)
    assert resp.status_code == 303
    assert AUTH_COOKIE not in resp.cookies
    assert client.get("/fs").status_code == 200


def test_host_link_upgrades_a_remote_session(client):
    client.get("/auth", params={"token": REMOTE_TOKEN}, follow_redirects=False)
    client.get("/auth", params={"token": HOST_TOKEN}, follow_redirects=False)
    assert client.get("/fs").status_code == 200
