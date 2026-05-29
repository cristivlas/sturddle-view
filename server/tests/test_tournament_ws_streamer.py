"""Unit tests for the tournament WS streamer (`_stream_queue_to_websocket`).

The streamer races three async tasks: queue.get() for normal frames,
queue.wait_terminal() for the sticky game-end payload, and
websocket.receive() for client disconnect. Terminal must win over recv
on close so the end-of-game banner reaches the client even when the WS
is tearing down in the same scheduler tick -- this regression suite
exists because banners were silently dropped before the fix.

Uses a hand-rolled fake WebSocket. The real ASGI path is exercised via
fastapi.TestClient elsewhere; here we want fine-grained control of
which task wins the asyncio.wait() race, which TestClient cannot give.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import WebSocketDisconnect

from sturddle_view.api.tournaments import _stream_queue_to_websocket
from sturddle_view.tournament.orchestrator import CoalescingQueue


class _FakeWS:
    """Minimal stand-in for fastapi.WebSocket.

    `sent` records every payload passed to `send_json`. `incoming` is a
    queue tests push to in order to script client->server signals: a
    plain dict is delivered; the sentinel `_CLOSE` makes `receive()`
    raise WebSocketDisconnect to simulate the client closing the WS.
    `recv_blocks_forever()` makes recv hang until cancel."""

    _CLOSE = object()

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self._block_forever = False

    def recv_blocks_forever(self) -> None:
        self._block_forever = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive(self) -> dict:
        if self._block_forever:
            await asyncio.Event().wait()
        msg = await self.incoming.get()
        if msg is self._CLOSE:
            raise WebSocketDisconnect(code=1000)
        return msg

    def disconnect(self) -> None:
        self.incoming.put_nowait(self._CLOSE)


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
    await asyncio.sleep(0.05)
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
    await asyncio.sleep(0.05)
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
    await asyncio.sleep(0.05)
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
    await asyncio.sleep(0.05)
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
    await asyncio.sleep(0.05)
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
        await asyncio.sleep(0.02)
        q.put_sentinel({"ended": True, "result": "1/2-1/2"})
        ws.disconnect()

    asyncio.create_task(_orchestrate())
    await asyncio.wait_for(
        _stream_queue_to_websocket(ws, q), timeout=0.5,
    )
    assert any(p.get("result") == "1/2-1/2" for p in ws.sent)


@pytest.mark.asyncio
async def test_streamer_cleans_up_tasks_after_terminal():
    """All three internal tasks must be cancelled before return so the
    asyncio loop has no lingering coroutines."""
    q = CoalescingQueue(maxsize=8)
    ws = _FakeWS()
    ws.recv_blocks_forever()

    task = asyncio.create_task(_stream_queue_to_websocket(ws, q))
    await asyncio.sleep(0.05)
    q.put_sentinel({"ended": True})
    await asyncio.wait_for(task, timeout=0.5)

    all_tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert all_tasks == []
