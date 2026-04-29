"""Agent API: agents push annotations/suggestions/commentary back to the server,
which broadcasts to all clients via the event bus.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_token
from ..events import Event

router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[Depends(require_token)])


@router.post("/annotation")
async def push_annotation(payload: dict, request: Request) -> dict:
    bus = request.app.state.event_bus
    await bus.publish(
        Event(
            kind="agent_annotation",
            game_id=payload.get("game_id"),
            payload=payload,
        )
    )
    return {"ok": True}
