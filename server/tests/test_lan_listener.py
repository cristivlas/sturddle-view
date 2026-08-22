from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest
import uvicorn
from uvicorn.server import ServerState

from sturddle_view import lan_listener as ll
from sturddle_view.config import LOOPBACK_HOST

# Any bindable address works as the "LAN" address: the listener never
# inspects it, and loopback keeps the test off the real network.
LAN_A = "127.0.0.1"
LAN_BIND = "192.168.1.5"


async def _app(scope, receive, send):  # never driven; sockets only
    raise NotImplementedError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((LOOPBACK_HOST, 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    """Just enough of uvicorn.Server for LanListener: a loaded Config, the
    shared ServerState, lifespan state, and the servers list it appends to."""
    # ws="wsproto": the "auto" choice imports websockets.legacy, which warns.
    config = uvicorn.Config(_app, host=LOOPBACK_HOST, port=_free_port(), log_config=None, ws="wsproto")
    config.load()
    srv = SimpleNamespace(
        config=config, server_state=ServerState(), lifespan=SimpleNamespace(state={}), servers=[],
    )
    yield srv
    for listening in srv.servers:
        listening.close()


@pytest.fixture
def wanted(monkeypatch):
    hosts = [LAN_A]
    monkeypatch.setattr(ll, "lan_hosts", lambda bind: list(hosts))
    return hosts


async def test_ensure_listens_once_per_host(server, wanted):
    listener = ll.LanListener(server, LOOPBACK_HOST)
    assert await listener.ensure() == [LAN_A]
    assert await listener.ensure() == [LAN_A]
    assert len(server.servers) == 1
    assert listener.hosts() == [LAN_A]
    socket.create_connection((LAN_A, server.config.port)).close()  # really listening


async def test_ensure_skips_failed_bind_and_retries_later(server, wanted):
    blocker = socket.socket()
    blocker.bind((LAN_A, server.config.port))
    blocker.listen(1)
    listener = ll.LanListener(server, LOOPBACK_HOST)
    try:
        assert await listener.ensure() == []
        assert server.servers == []
        assert listener.hosts() == []
    finally:
        blocker.close()
    assert await listener.ensure() == [LAN_A]
    assert len(server.servers) == 1


async def test_ensure_reports_only_current_addresses(server, wanted):
    listener = ll.LanListener(server, LOOPBACK_HOST)
    assert await listener.ensure() == [LAN_A]
    wanted.clear()  # Wi-Fi gone: the socket stays, the QR must not use it
    assert await listener.ensure() == []
    assert listener.hosts() == [LAN_A]
    assert len(server.servers) == 1


async def test_ensure_concurrent_calls_bind_once(server, wanted):
    listener = ll.LanListener(server, LOOPBACK_HOST)
    results = await asyncio.gather(listener.ensure(), listener.ensure())
    assert results == [[LAN_A], [LAN_A]]
    assert len(server.servers) == 1


async def test_non_loopback_bind_needs_no_extra_listener(server, wanted):
    listener = ll.LanListener(server, LAN_BIND)
    assert await listener.ensure() == [LAN_A]
    assert listener.hosts() == [LAN_A]
    assert server.servers == []
