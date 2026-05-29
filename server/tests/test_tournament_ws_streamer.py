"""Unit tests for the tournament WS streamer (`_stream_queue_to_websocket`).

Terminal must win over recv on close so the end-of-game banner reaches
the client even when the WS tears down in the same scheduler tick.
Synchronization via `_FakeWS.recv_started` (set on first await), not
sleeps.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import WebSocketDisconnect

from sturddle_view.api.tournaments import _stream_queue_to_websocket
from sturddle_view.tournament.orchestrator import CoalescingQueue


class _FakeWS:
    """Stand-in for fastapi.WebSocket. `recv_started` fires on first
    `receive()` await -- deterministic "streamer is parked" signal.
    `incoming` carries client->server signals; `_CLOSE` raises
    WebSocketDisconnect. `send_called` fires on every send_json so
    tests can wait for "N frames flushed" without polling."""

    _CLOSE = object()

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.recv_started: asyncio.Event = asyncio.Event()
        self.send_called: asyncio.Event = asyncio.Event()
        self._block_forever = False

    def recv_blocks_forever(self) -> None:
        self._block_forever = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)
        self.send_called.set()
        self.send_called.clear()

    async def receive(self) -> dict:
        self.recv_started.set()
        if self._block_forever:
            await asyncio.Event().wait()
        msg = await self.incoming.get()
        if msg is self._CLOSE:
            raise WebSocketDisconnect(code=1000)
        return msg

    def disconnect(self) -> None:
        self.incoming.put_nowait(self._CLOSE)


async def _wait_until_sent(ws: _FakeWS, n: int, *, timeout: float = 0.5) -> None:
    """Wait until ws.sent has at least n frames. Uses the send_called
    edge-trigger instead of polling so the test stays deterministic."""
    while len(ws.sent) < n:
        await asyncio.wait_for(ws.send_called.wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_streamer_delivers_terminal_when_already_set_on_attach():
    """Race-on-attach: pair already dissolved when client connects.
    subscribe_to_game pushes the sentinel before returning the queue;
    the streamer must send it without entering the wait loop."""
    q = CoalescingQueue(maxsize=8)
    q.put_sentinel({"ended": True, "result": "1-0"})
    ws = _FakeWS()
    ws.recv_blocks_forever()

    await asyncio.wait_for(
        _stream_queue_to_websocket(ws, q), timeout=0.5,
    )

    assert ws.sent == [{"ended": True, "result": "1-0"}]


@pytest.mark.asyncio
async def test_streamer_forwards_normal_frames_with_parsed_enrichment():
    q = CoalescingQueue(maxsize=8)
    q.put_other({"proxy_id": "p1", "line": "position startpos"})
    ws = _FakeWS()
    ws.recv_blocks_forever()

    task = asyncio.create_task(_stream_queue_to_websocket(ws, q))
    # Wait until the frame is flushed before posting the sentinel; the
    # sentinel races get_task otherwise and may win.
    await _wait_until_sent(ws, 1)
    q.put_sentinel({"ended": True})
    await asyncio.wait_for(task, timeout=0.5)

    assert ws.sent[0]["line"] == "position startpos"
    assert ws.sent[0]["parsed"]["kind"] == "position"
    assert ws.sent[-1] == {"ended": True}


@pytest.mark.asyncio
async def test_streamer_preserves_caller_supplied_parsed_field():
    """Orchestrator may have already parsed for pairing detection. The
    streamer must not re-parse and overwrite the original 'parsed'."""
    q = CoalescingQueue(maxsize=8)
    q.put_other({
        "proxy_id": "p1",
        "line": "position startpos",
        "parsed": {"kind": "position", "tag": "ALREADY-PARSED"},
    })
    ws = _FakeWS()
    ws.recv_blocks_forever()

    task = asyncio.create_task(_stream_queue_to_websocket(ws, q))
    await _wait_until_sent(ws, 1)
    q.put_sentinel({"ended": True})
    await asyncio.wait_for(task, timeout=0.5)

    assert ws.sent[0]["parsed"]["tag"] == "ALREADY-PARSED"


@pytest.mark.asyncio
async def test_streamer_delivers_terminal_mid_stream():
    """Terminal arrives while the streamer is parked in asyncio.wait.
    Must wake up via term_task and flush it before exiting."""
    q = CoalescingQueue(maxsize=8)
    ws = _FakeWS()
    ws.recv_blocks_forever()

    task = asyncio.create_task(_stream_queue_to_websocket(ws, q))
    await asyncio.wait_for(ws.recv_started.wait(), timeout=0.5)
    assert ws.sent == []
    q.put_sentinel({"ended": True, "result": "0-1"})

    await asyncio.wait_for(task, timeout=0.5)
    assert ws.sent == [{"ended": True, "result": "0-1"}]


@pytest.mark.asyncio
async def test_streamer_exits_on_client_disconnect_without_terminal():
    """Client tears down WS, no terminal in flight. The streamer must
    exit cleanly with no spurious sent payloads."""
    q = CoalescingQueue(maxsize=8)
    ws = _FakeWS()

    task = asyncio.create_task(_stream_queue_to_websocket(ws, q))
    await asyncio.wait_for(ws.recv_started.wait(), timeout=0.5)
    ws.disconnect()

    await asyncio.wait_for(task, timeout=0.5)
    assert ws.sent == []


@pytest.mark.asyncio
async def test_streamer_flushes_terminal_when_recv_wins_race():
    """The core regression: client disconnect and terminal arrive in
    the same scheduler tick. Old handler dropped the sentinel because
    recv_task won asyncio.wait. New streamer must still flush the
    terminal because term_task is in the wait set."""
    q = CoalescingQueue(maxsize=8)
    ws = _FakeWS()

    task = asyncio.create_task(_stream_queue_to_websocket(ws, q))
    await asyncio.wait_for(ws.recv_started.wait(), timeout=0.5)
    q.put_sentinel({"ended": True, "result": "1-0"})
    ws.disconnect()

    await asyncio.wait_for(task, timeout=0.5)
    assert {"ended": True, "result": "1-0"} in ws.sent


@pytest.mark.asyncio
async def test_streamer_flushes_terminal_on_websocket_disconnect_exception():
    """websocket.receive() raises WebSocketDisconnect mid-loop. If the
    terminal was already set (e.g. server-side dissolve completed in the
    same tick), the streamer's WebSocketDisconnect handler must still
    flush it."""
    q = CoalescingQueue(maxsize=8)
    ws = _FakeWS()

    async def _orchestrate():
        await ws.recv_started.wait()
        q.put_sentinel({"ended": True, "result": "1/2-1/2"})
        ws.disconnect()

    asyncio.create_task(_orchestrate())
    await asyncio.wait_for(
        _stream_queue_to_websocket(ws, q), timeout=0.5,
    )
    assert any(p.get("result") == "1/2-1/2" for p in ws.sent)


@pytest.mark.asyncio
async def test_streamer_cleans_up_tasks_after_terminal():
    """No lingering internal tasks after the streamer returns. Snapshot
    the foreign-task set before+after so unrelated tasks (pytest-asyncio
    fixtures, anyio threads) don't poison the assertion."""
    q = CoalescingQueue(maxsize=8)
    ws = _FakeWS()
    ws.recv_blocks_forever()

    before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

    task = asyncio.create_task(_stream_queue_to_websocket(ws, q))
    await asyncio.wait_for(ws.recv_started.wait(), timeout=0.5)
    q.put_sentinel({"ended": True})
    await asyncio.wait_for(task, timeout=0.5)
    # Yield once so any cancellation scheduled inside the streamer's
    # finally has a chance to clear from all_tasks.
    await asyncio.sleep(0)

    after = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    leaked = after - before
    assert leaked == set(), f"streamer leaked tasks: {leaked}"
