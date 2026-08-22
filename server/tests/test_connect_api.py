from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from sturddle_view import netinfo
from sturddle_view.api import connect as connect_api
from sturddle_view.app import create_app
from sturddle_view.config import LOOPBACK_HOST, WILDCARD_HOST, Settings
from sturddle_view.engines import EngineRegistry

TOKEN = "test-token"
LOCAL = ("127.0.0.1", 50000)
OWN_IP = ("192.168.1.5", 50000)  # this machine, reached via its LAN address
REMOTE = ("192.168.1.9", 50000)
LAN = ["192.168.1.5", "172.18.0.1"]
QR_URI_PREFIX = "data:image/svg+xml"


class FakeListener:
    """Stands in for lan_listener.LanListener: ensure() "binds" a canned list."""

    def __init__(self, result):
        self.result = list(result)
        self.bound: list[str] = []

    def hosts(self):
        return list(self.bound)

    async def ensure(self):
        self.bound = list(self.result)
        return list(self.result)


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    # The QR payload is opaque; capture the URL it would encode instead.
    monkeypatch.setattr(connect_api, "_qr_data_uri", lambda url: f"qr:{url}")
    # Pin this machine's interface addresses so "local" is deterministic.
    monkeypatch.setattr(netinfo, "reachable_hosts", lambda bind: [OWN_IP[0], LOOPBACK_HOST])

    def _make(client_addr, *, host=LOOPBACK_HOST, auth_disabled=False, listener=None):
        settings = Settings(token=TOKEN, host=host, auth_disabled=auth_disabled)
        registry = EngineRegistry(path=tmp_path / "engines.json")
        app = create_app(settings=settings, engine_registry=registry)
        app.state.lan_listener = listener
        c = TestClient(app, client=client_addr)
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        return c
    return _make


def _qr_for(host: str) -> str:
    return f"qr:http://{host}:8765/auth?token={TOKEN}"


def test_qr_data_uri_is_inline_svg():
    assert connect_api._qr_data_uri("http://example").startswith(QR_URI_PREFIX)


def test_get_connect_is_local_only_for_local_clients(make_client):
    with make_client(LOCAL) as c:
        assert c.get("/connect").json() == {"local": True}
    with make_client(OWN_IP) as c:
        assert c.get("/connect").json() == {"local": True}
    with make_client(REMOTE) as c:
        assert c.get("/connect").json() == {"local": False}


def test_connect_requires_token(make_client):
    with make_client(LOCAL) as c:
        del c.headers["Authorization"]
        assert c.get("/connect").status_code == 401
        assert c.post("/connect/lan").status_code == 401


def test_enable_lan_rejects_remote_clients(make_client):
    with make_client(REMOTE, host=WILDCARD_HOST) as c:
        assert c.post("/connect/lan").status_code == 403


def test_enable_lan_server_mode_loopback_bind(make_client):
    with make_client(LOCAL) as c:
        assert c.post("/connect/lan").json() == {
            "qr": None, "reason": connect_api.REASON_LOOPBACK_BIND,
        }


def test_enable_lan_server_mode_wildcard_bind(make_client, monkeypatch):
    monkeypatch.setattr(connect_api, "lan_hosts", lambda bind: LAN)
    with make_client(LOCAL, host=WILDCARD_HOST) as c:
        assert c.post("/connect/lan").json() == {"qr": _qr_for(LAN[0]), "reason": None}


def test_enable_lan_server_mode_offline(make_client, monkeypatch):
    monkeypatch.setattr(connect_api, "lan_hosts", lambda bind: [])
    with make_client(LOCAL, host=WILDCARD_HOST) as c:
        assert c.post("/connect/lan").json() == {"qr": None, "reason": connect_api.REASON_OFFLINE}


def test_enable_lan_desktop_binds_on_demand(make_client):
    listener = FakeListener(LAN)
    with make_client(LOCAL, listener=listener) as c:
        assert c.post("/connect/lan").json() == {"qr": _qr_for(LAN[0]), "reason": None}
    assert listener.bound == LAN


def test_enable_lan_desktop_bind_failed_is_not_offline(make_client, monkeypatch):
    monkeypatch.setattr(connect_api, "lan_hosts", lambda bind: LAN)
    with make_client(LOCAL, listener=FakeListener([])) as c:
        assert c.post("/connect/lan").json()["reason"] == connect_api.REASON_BIND_FAILED


def test_enable_lan_desktop_offline(make_client, monkeypatch):
    monkeypatch.setattr(connect_api, "lan_hosts", lambda bind: [])
    with make_client(LOCAL, listener=FakeListener([])) as c:
        assert c.post("/connect/lan").json()["reason"] == connect_api.REASON_OFFLINE


def test_enable_lan_warns_once_when_auth_disabled(make_client, caplog):
    with make_client(LOCAL, auth_disabled=True, listener=FakeListener(LAN)) as c:
        with caplog.at_level(logging.WARNING, logger=connect_api.log.name):
            c.post("/connect/lan")
            c.post("/connect/lan")  # nothing newly exposed: no second warning
    warnings = [r for r in caplog.records if "AUTH DISABLED" in r.getMessage()]
    assert len(warnings) == 1
    assert LAN[0] in warnings[0].getMessage()


def test_enable_lan_no_warning_when_auth_on(make_client, caplog):
    with make_client(LOCAL, listener=FakeListener(LAN)) as c:
        with caplog.at_level(logging.WARNING, logger=connect_api.log.name):
            c.post("/connect/lan")
    assert not [r for r in caplog.records if "AUTH DISABLED" in r.getMessage()]
