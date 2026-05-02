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
    probe_engine,
    resolve_selected,
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
    # Optional: when omitted/blank, the server defaults to the engine's UCI
    # `id name`, falling back to the binary basename if the engine doesn't
    # respond. The user can rename later via PATCH.
    name: str | None = None
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


async def _ensure_schema(reg: EngineRegistry, e: Engine) -> Engine:
    """Lazily capture the option schema if missing.

    Engines registered before schema capture was added have an empty
    `option_schema`. We capture and persist on first encounter so the
    UI never has to show "no options" for an engine that actually has
    them. Best-effort: a failed capture leaves the schema empty and is
    retried next call.
    """
    if e.option_schema:
        return e
    _uci_name, schema = await probe_engine(e.path)
    if not schema:
        return e
    try:
        return reg.update(e.id, option_schema=schema)
    except EngineNotFoundError:
        return e


@router.get("")
async def list_engines(request: Request) -> dict:
    reg = _registry(request)
    out = []
    for e in reg.list():
        e = await _ensure_schema(reg, e)
        out.append(_serialize(e))
    return {
        "engines": out,
        "selected_id": reg.selected_id,
    }


@router.post("", status_code=201)
async def add_engine(payload: EngineCreate, request: Request) -> dict:
    reg = _registry(request)
    resolved_path = _validate_engine_path(payload.path)
    uci_name, schema = await probe_engine(resolved_path)
    name = (payload.name or "").strip() or uci_name or Path(resolved_path).name
    try:
        e = reg.add(
            name=name,
            path=resolved_path,
            options=payload.options,
            option_schema=schema,
        )
    except DuplicateEngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # First engine added with nothing selected -> auto-select it. Saves the
    # user a redundant click; any subsequent add leaves the active engine
    # alone.
    if reg.selected_id is None:
        reg.select(e.id)
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
async def select_engine(engine_id: str, request: Request) -> dict:
    reg = _registry(request)
    try:
        reg.select(engine_id)
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    # Eager swap so mid-analysis (no /game/* call follows) takes effect now.
    s = request.app.state
    hve = getattr(s, "hve", None)
    if hve is not None:
        path, name, options = resolve_selected(s.engines, s.settings)
        if path is not None and hve.engine_path != path:
            await hve.swap_engine(path)
            hve.set_engine_name(name)
            hve.set_engine_options(options)
    return {"selected_id": engine_id}


@router.post("/{engine_id}/refresh-schema")
async def refresh_engine_schema(engine_id: str, request: Request) -> dict:
    """Re-spawn the engine to re-capture its UCI option list.

    Useful after an engine binary upgrade — option set, ranges, or
    defaults may have changed.
    """
    reg = _registry(request)
    try:
        e = reg.get(engine_id)
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    _uci_name, schema = await probe_engine(e.path)
    if not schema:
        raise HTTPException(
            status_code=502, detail="could not capture options from engine"
        )
    try:
        e = reg.update(engine_id, option_schema=schema)
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    return _serialize(e)
