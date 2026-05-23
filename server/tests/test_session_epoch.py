"""Session-epoch contract for the WS event envelope.

Every outgoing event carries `session_epoch`. The client tracks the
first epoch it sees and reloads on a mismatch -- that's how a server
restart resyncs stale UI state (view-mode cursor, dismissed toasts,
edit drafts) the new server session can't honor.
"""
from __future__ import annotations

import re

from sturddle_view.api.ws import _event_to_json
from sturddle_view.events import SESSION_EPOCH, Event


UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def test_session_epoch_is_uuid_hex():
    assert isinstance(SESSION_EPOCH, str)
    assert UUID_HEX_RE.match(SESSION_EPOCH), SESSION_EPOCH


def test_event_to_json_includes_session_epoch():
    out = _event_to_json(Event(kind="board_update", payload={}, game_id="g1"))
    assert out["session_epoch"] == SESSION_EPOCH


def test_session_epoch_stable_across_calls():
    a = _event_to_json(Event(kind="engine_info", payload={"x": 1}))
    b = _event_to_json(Event(kind="clock_tick", payload={"y": 2}))
    assert a["session_epoch"] == b["session_epoch"] == SESSION_EPOCH
