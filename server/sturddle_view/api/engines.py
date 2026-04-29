from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_token
from ..engines import (
    DuplicateEngineError,
    Engine,
    EngineNotFoundError,
    EngineRegistry,
)

router = APIRouter(prefix="/engines", tags=["engines"], dependencies=[Depends(require_token)])


class EngineCreate(BaseModel):
    name: str
    path: str
    options: dict[str, Any] = {}


class EngineUpdate(BaseModel):
    name: str | None = None
    path: str | None = None
    options: dict[str, Any] | None = None


def _registry(request: Request) -> EngineRegistry:
    return request.app.state.engines


def _serialize(e: Engine) -> dict:
    return asdict(e)


@router.get("")
def list_engines(request: Request) -> dict:
    reg = _registry(request)
    selected = request.app.state.selected_engine_id
    return {
        "engines": [_serialize(e) for e in reg.list()],
        "selected_id": selected,
    }


@router.post("", status_code=201)
def add_engine(payload: EngineCreate, request: Request) -> dict:
    reg = _registry(request)
    try:
        e = reg.add(name=payload.name, path=payload.path, options=payload.options)
    except DuplicateEngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize(e)


@router.patch("/{engine_id}")
def update_engine(engine_id: str, payload: EngineUpdate, request: Request) -> dict:
    reg = _registry(request)
    try:
        e = reg.update(
            engine_id, name=payload.name, path=payload.path, options=payload.options
        )
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    return _serialize(e)


@router.delete("/{engine_id}", status_code=204)
def remove_engine(engine_id: str, request: Request) -> None:
    reg = _registry(request)
    try:
        reg.remove(engine_id)
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    if request.app.state.selected_engine_id == engine_id:
        request.app.state.selected_engine_id = None


@router.post("/{engine_id}/select")
def select_engine(engine_id: str, request: Request) -> dict:
    reg = _registry(request)
    try:
        reg.get(engine_id)
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    request.app.state.selected_engine_id = engine_id
    return {"selected_id": engine_id}
