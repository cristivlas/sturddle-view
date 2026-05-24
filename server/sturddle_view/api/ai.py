"""AI analysis API: start/cancel a coordinator turn.

Walking-skeleton scope: a POST kicks the coordinator (which streams canned
chunks back via the websocket bus as `ai_info` events). Real provider
selection from settings, mode/state path dispatch, and tools land in
later cycles. Cancel is wired so the UI can hard-stop a turn even before
real provider integration.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"], dependencies=[Depends(require_token)])


def _coordinator(request: Request):
    coord = getattr(request.app.state, "ai_coordinator", None)
    if coord is None:
        raise HTTPException(status_code=503, detail="AI coordinator not initialized")
    return coord


def _log_task_exception(task: asyncio.Task) -> None:
    # Surface unhandled errors from the fire-and-forget run() task --
    # otherwise they vanish silently when the task is GC'd.
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.exception("AI coordinator run failed", exc_info=exc)


@router.post("/start")
async def start(request: Request) -> dict:
    s = request.app.state.settings
    if not s.ai_enabled:
        raise HTTPException(status_code=400, detail="AI analysis is disabled in settings")
    coord = _coordinator(request)
    game_id = getattr(request.app.state.hve, "game_id", None) if request.app.state.hve else None
    # Pin the task on app.state so the event loop holds a strong ref --
    # asyncio GC can otherwise reap an unreferenced task mid-flight.
    task = asyncio.create_task(coord.run(game_id=game_id))
    task.add_done_callback(_log_task_exception)
    request.app.state.ai_task = task
    return {"ok": True}


@router.post("/cancel")
async def cancel(request: Request) -> dict:
    coord = _coordinator(request)
    await coord.cancel()
    return {"ok": True}
