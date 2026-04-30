from __future__ import annotations

import asyncio
import dataclasses
import hmac
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from ..events import Event, EventBus

router = APIRouter()
log = logging.getLogger(__name__)


def _event_to_json(event: Event) -> dict:
    return {"kind": event.kind, "game_id": event.game_id, "payload": event.payload}


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query(default="")) -> None:
    settings = websocket.app.state.settings
    if not settings.auth_disabled and not hmac.compare_digest(token, settings.token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    bus: EventBus = websocket.app.state.event_bus
    queue = await bus.subscribe()
    task = asyncio.current_task()
    websocket.app.state.ws_tasks.add(task)

    hve = websocket.app.state.hve
    if hve is not None:
        try:
            for event in hve.snapshot_events():
                await websocket.send_json(_event_to_json(event))
        except Exception:
            log.exception("failed to send state snapshot on ws connect")

    async def _drain_recv() -> None:
        # We don't expect client->server messages yet, but we MUST be reading from
        # the socket so that disconnects propagate as WebSocketDisconnect rather
        # than leaving the task wedged on queue.get() forever.
        while True:
            await websocket.receive()

    recv_task = asyncio.create_task(_drain_recv())
    try:
        while True:
            get_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {get_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if recv_task in done:
                # Either disconnect or unexpected message — stop.
                get_task.cancel()
                break
            event = get_task.result()
            await websocket.send_json(_event_to_json(event))
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("ws handler error")
    finally:
        recv_task.cancel()
        websocket.app.state.ws_tasks.discard(task)
        await bus.unsubscribe(queue)
        try:
            await websocket.close()
        except Exception:
            pass
