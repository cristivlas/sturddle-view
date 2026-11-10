"""DNS-rebinding guard: requests whose Host header doesn't name this machine
are refused, even when Origin matches Host (a re-pointed attacker domain)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sturddle_view import netinfo
from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from .conftest import REGISTRY_FILE

LAN_IP = "192.168.1.5"
EXTRA_NAME = "mypc.tail1234.ts.net"
EVIL = "evil.example:8765"


@pytest.fixture
def client(tmp_path):
    settings = Settings(auth_disabled=True, allowed_hosts=f" {EXTRA_NAME.upper()} , ")
    registry = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    with TestClient(create_app(settings=settings, engine_registry=registry)) as c:
        yield c


@pytest.mark.parametrize("host", [
    "127.0.0.1:8765", "localhost:8765", "LocalHost", "[::1]:8765", EXTRA_NAME,
])
def test_this_machine_is_served(client, host):
    assert client.get("/healthz", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", [EVIL, "127.0.0.1.nip.io", f"{LAN_IP}:8765", "", "[bad"])
def test_other_names_are_refused(client, host):
    assert client.get("/healthz", headers={"host": host}).status_code == 400


def test_rebound_page_is_refused_even_with_matching_origin(client):
    resp = client.put("/settings", json={}, headers={"host": EVIL, "origin": f"http://{EVIL}"})
    assert resp.status_code == 400


def test_websocket_with_foreign_host_is_closed(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"host": EVIL}) as ws:
            ws.receive_json()


def test_websocket_with_local_host_connects(client):
    with client.websocket_connect("/ws", headers={"host": "127.0.0.1:8765"}):
        pass


@pytest.mark.parametrize(("host", "local"), [
    (f"{LAN_IP}:8765", LAN_IP),
    ("[fd00::5]:8765", "fd00::5"),
    (f"{LAN_IP}:8765", f"::ffff:{LAN_IP}"),
])
def test_the_address_the_request_arrived_on_is_served(host, local):
    assert netinfo.host_allowed(Settings(allowed_hosts=""), host, local)


def test_another_address_of_ours_is_refused_on_this_socket():
    assert not netinfo.host_allowed(Settings(allowed_hosts=""), f"{LAN_IP}:8765", "10.0.0.2")


@pytest.mark.parametrize("bind", ["mybox.local", "fd00::5"])
def test_the_bind_name_from_the_banner_is_served(bind):
    settings = Settings(host=bind, allowed_hosts="")
    host = f"[{bind}]:8765" if ":" in bind else f"{bind}:8765"
    assert netinfo.host_allowed(settings, host, None)


def test_wildcard_bind_is_not_a_host_name():
    settings = Settings(host="0.0.0.0", allowed_hosts="")
    assert not netinfo.host_allowed(settings, "0.0.0.0:8765", None)
