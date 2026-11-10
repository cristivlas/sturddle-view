"""uvicorn's WSProtocol.shutdown, as patched by the app: the 1012 close goes
out only in the states wsproto accepts it in, so a socket the client or the
handler already closed can't raise LocalProtocolError on server shutdown."""
from __future__ import annotations

import pytest

pytest.importorskip("wsproto")

from uvicorn.protocols.websockets import wsproto_impl  # noqa: E402
from wsproto.connection import ConnectionState  # noqa: E402

from sturddle_view import app as app_mod  # noqa: E402

CLOSE_FRAME = b"close-frame"


class _Conn:
    def __init__(self, state):
        self.state = state
        self.sent = []

    def send(self, event):
        self.sent.append(event)
        return CLOSE_FRAME


class _Transport:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(data)

    def close(self):
        self.closed = True


class _Queue:
    def __init__(self):
        self.items = []

    def put_nowait(self, item):
        self.items.append(item)


class _Protocol:
    """The WSProtocol attributes shutdown() touches."""

    def __init__(self, state):
        self.handshake_complete = True
        self.conn = _Conn(state)
        self.transport = _Transport()
        self.queue = _Queue()

    def stop_keepalive(self):
        pass

    def send_500_response(self):
        raise AssertionError("handshake was complete")


@pytest.fixture(autouse=True)
def _guard():
    app_mod._install_uvicorn_ws_shutdown_state_guard()


@pytest.mark.parametrize("state", [ConnectionState.OPEN, ConnectionState.REMOTE_CLOSING])
def test_close_is_sent_where_wsproto_allows_it(state):
    proto = _Protocol(state)
    wsproto_impl.WSProtocol.shutdown(proto)
    assert proto.transport.written == [CLOSE_FRAME]
    assert proto.transport.closed


@pytest.mark.parametrize("state", [ConnectionState.LOCAL_CLOSING, ConnectionState.CLOSED])
def test_no_close_on_an_already_closing_socket(state):
    proto = _Protocol(state)
    wsproto_impl.WSProtocol.shutdown(proto)
    assert proto.conn.sent == [] and proto.transport.written == []
    assert proto.transport.closed
