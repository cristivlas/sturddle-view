"""Unit tests for the proxy ``Broadcaster`` want-info gate.

These exercise ``_apply_want_info`` directly with in-memory httpx
responses -- no network, no engine, no server. The gate is fail-open:
only a valid boolean body flips it; anything else leaves it untouched."""
from __future__ import annotations

import httpx
import pytest

from sturddle_view.tournament.proxy import _WANT_INFO_KEY, Broadcaster


@pytest.fixture
def bc():
    """A Broadcaster pointed at a dead URL. We never enqueue a real post,
    so no socket is opened. Teardown stops the worker with the sentinel
    directly (no network) to avoid stderr noise from a refused connect."""
    b = Broadcaster("http://127.0.0.1:9/none", "p-test", "secret", "EngineA")
    yield b
    b._post_q.put(None)
    b._post_stopped.wait(timeout=2)


def _resp(status: int, *, json=None, content: bytes | None = None) -> httpx.Response:
    if json is not None:
        return httpx.Response(status, json=json)
    return httpx.Response(status, content=content or b"")


def test_defaults_open(bc):
    assert bc.want_info is True


def test_204_leaves_flag_untouched(bc):
    bc._apply_want_info(_resp(204))
    assert bc.want_info is True


def test_empty_body_leaves_flag_untouched(bc):
    bc._apply_want_info(_resp(200, content=b""))
    assert bc.want_info is True


def test_flips_false_then_true(bc):
    bc._apply_want_info(_resp(200, json={_WANT_INFO_KEY: False}))
    assert bc.want_info is False
    bc._apply_want_info(_resp(200, json={_WANT_INFO_KEY: True}))
    assert bc.want_info is True


def test_garbage_body_is_failopen(bc):
    bc._apply_want_info(_resp(200, json={_WANT_INFO_KEY: False}))
    assert bc.want_info is False
    # Non-JSON, missing key, and non-bool value all leave the last
    # good value in place rather than crashing or blinding the watcher.
    bc._apply_want_info(_resp(200, content=b"not json"))
    bc._apply_want_info(_resp(200, json={"other": 1}))
    bc._apply_want_info(_resp(200, json={_WANT_INFO_KEY: "yes"}))
    assert bc.want_info is False
