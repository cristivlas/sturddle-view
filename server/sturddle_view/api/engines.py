from __future__ import annotations

import os
import sys
from dataclasses import asdict
from pathlib import Path
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


def _validate_engine_path(raw: str) -> str:
    """Resolve and check an engine path; raise HTTPException if unusable.

    Returns the resolved string path.
    """
    if not raw or not raw.strip():
        raise HTTPException(status_code=400, detail="engine path is empty")
    p = Path(raw).expanduser()
    try:
        p = p.resolve(strict=False)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"bad path: {e}") from e
    if not p.exists():
        raise HTTPException(status_code=400, detail=f"path does not exist: {p}")
    if p.is_dir():
        raise HTTPException(status_code=400, detail=f"path is a directory, not a file: {p}")
    if not p.is_file():
        raise HTTPException(status_code=400, detail=f"path is not a regular file: {p}")
    if not sys.platform.startswith("win") and not os.access(p, os.X_OK):
        raise HTTPException(status_code=400, detail=f"path is not executable: {p}")
    return str(p)

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
    return {
        "engines": [_serialize(e) for e in reg.list()],
        "selected_id": reg.selected_id,
    }


@router.post("", status_code=201)
def add_engine(payload: EngineCreate, request: Request) -> dict:
    reg = _registry(request)
    resolved_path = _validate_engine_path(payload.path)
    try:
        e = reg.add(name=payload.name, path=resolved_path, options=payload.options)
    except DuplicateEngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize(e)


@router.patch("/{engine_id}")
def update_engine(engine_id: str, payload: EngineUpdate, request: Request) -> dict:
    reg = _registry(request)
    new_path = _validate_engine_path(payload.path) if payload.path is not None else None
    try:
        e = reg.update(
            engine_id, name=payload.name, path=new_path, options=payload.options
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


@router.post("/{engine_id}/select")
def select_engine(engine_id: str, request: Request) -> dict:
    reg = _registry(request)
    try:
        reg.select(engine_id)
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    return {"selected_id": engine_id}
