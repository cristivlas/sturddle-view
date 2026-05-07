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
    InvalidLaunchProfileError,
    probe_engine,
    resolve_selected,
    validate_launch_profile,
)
from ..tournament.store import STATUS_DONE, TournamentStore


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
    args: list[str] = []
    env: dict[str, str] = {}


class EngineUpdate(BaseModel):
    name: str | None = None
    path: str | None = None
    options: dict[str, Any] | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None


class EngineProbe(BaseModel):
    """Trial-spawn payload — does not touch the registry. Used by the
    Engine Settings dialog to probe with the user's *in-progress* launch
    profile (current path/args/env edits) before saving."""
    path: str
    args: list[str] = []
    env: dict[str, str] = {}


def _validate_launch(args: list[str] | None, env: dict[str, str] | None) -> None:
    """Wrap the shared ``validate_launch_profile`` and re-raise as an HTTP 400."""
    try:
        validate_launch_profile(args, env)
    except InvalidLaunchProfileError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _registry(request: Request) -> EngineRegistry:
    return request.app.state.engines


def _engine_locks(request: Request) -> dict[str, list[dict]]:
    """Map engine registry id -> list of {name, status} for non-DONE tournaments."""
    ts: TournamentStore = getattr(request.app.state, "tournament_store", None)
    if ts is None:
        return {}
    locks: dict[str, list[dict]] = {}
    for t in ts.list():
        if t.status == STATUS_DONE:
            continue
        for ref in t.engines or []:
            eng_id = ref.get("id") if isinstance(ref, dict) else getattr(ref, "id", None)
            if eng_id:
                locks.setdefault(eng_id, []).append({"name": t.name, "status": t.status})
    return locks


def _check_engine_locked(engine_id: str, request: Request) -> None:
    """Raise 409 if the engine is referenced by any non-DONE tournament."""
    ts: TournamentStore = getattr(request.app.state, "tournament_store", None)
    if ts is None:
        return
    locks = _engine_locks(request)
    refs = locks.get(engine_id, [])
    if refs:
        names = ", ".join(f"{r['name']} ({r['status']})" for r in refs)
        raise HTTPException(status_code=409, detail=f"engine in use by: {names}")


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
    _uci_name, schema, _err = await probe_engine(e.path)
    if not schema:
        return e
    try:
        return reg.update(e.id, option_schema=schema)
    except EngineNotFoundError:
        return e


@router.get("")
async def list_engines(request: Request) -> dict:
    reg = _registry(request)
    locks = _engine_locks(request)
    out = []
    for e in reg.list():
        e = await _ensure_schema(reg, e)
        d = _serialize(e)
        d["locked"] = locks.get(e.id, [])
        out.append(d)
    return {
        "engines": out,
        "selected_id": reg.selected_id,
    }


@router.post("", status_code=201)
async def add_engine(payload: EngineCreate, request: Request) -> dict:
    reg = _registry(request)
    resolved_path = _validate_engine_path(payload.path)
    _validate_launch(payload.args, payload.env)
    uci_name, schema, probe_error = await probe_engine(
        resolved_path, args=list(payload.args), env=dict(payload.env),
    )
    user_supplied = bool((payload.name or "").strip())
    name = (payload.name or "").strip() or uci_name or Path(resolved_path).name
    try:
        e = reg.add(
            name=name,
            path=resolved_path,
            options=payload.options,
            option_schema=schema,
            args=list(payload.args),
            env=dict(payload.env),
            auto_suffix=not user_supplied,
        )
    except DuplicateEngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # First engine added with nothing selected -> auto-select it. Saves the
    # user a redundant click; any subsequent add leaves the active engine
    # alone.
    if reg.selected_id is None:
        reg.select(e.id)
    body = _serialize(e)
    if probe_error:
        # Surface the probe failure so the UI can warn the user. The engine
        # is still registered (best-effort), but with no UCI options known —
        # without this, the per-engine dialog would silently look "empty".
        body["probe_error"] = probe_error
    return body


@router.patch("/{engine_id}")
def update_engine(engine_id: str, payload: EngineUpdate, request: Request) -> dict:
    _check_engine_locked(engine_id, request)
    reg = _registry(request)
    new_path = _validate_engine_path(payload.path) if payload.path is not None else None
    _validate_launch(payload.args, payload.env)
    try:
        e = reg.update(
            engine_id,
            name=payload.name,
            path=new_path,
            options=payload.options,
            args=payload.args,
            env=payload.env,
        )
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    except DuplicateEngineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize(e)


@router.delete("/{engine_id}", status_code=204)
def remove_engine(engine_id: str, request: Request) -> None:
    _check_engine_locked(engine_id, request)
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
        launch = resolve_selected(s.engines, s.settings)
        if launch.path is not None and hve.engine_path != launch.path:
            await hve.swap_engine(launch.path)
            hve.set_engine_name(launch.name)
            hve.set_engine_options(launch.options)
            hve.set_engine_args(launch.args)
            hve.set_engine_env(launch.env)
    return {"selected_id": engine_id}


@router.post("/{engine_id}/refresh-schema")
async def refresh_engine_schema(engine_id: str, request: Request) -> dict:
    """Re-spawn the engine to re-capture its UCI option list.

    Uses the engine's *saved* launch profile (path/args/env). For probing
    with in-progress edits before save, use POST /engines/probe.
    """
    _check_engine_locked(engine_id, request)
    reg = _registry(request)
    try:
        e = reg.get(engine_id)
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    _uci_name, schema, probe_error = await probe_engine(
        e.path, args=list(e.args or []), env=dict(e.env or {}),
    )
    if not schema:
        detail = "could not capture options from engine"
        if probe_error:
            detail = f"{detail}: {probe_error}"
        raise HTTPException(status_code=502, detail=detail)
    try:
        e = reg.update(engine_id, option_schema=schema)
    except EngineNotFoundError as exc:
        raise HTTPException(status_code=404, detail="engine not found") from exc
    return _serialize(e)


@router.post("/probe")
async def probe(payload: EngineProbe) -> dict:
    """Trial-spawn a launch profile; return UCI name + option schema.

    Does not touch the registry. Lets the Engine Settings dialog probe
    with the user's current edits to path/args/env before they hit Save.
    """
    resolved_path = _validate_engine_path(payload.path)
    _validate_launch(payload.args, payload.env)
    uci_name, schema, probe_error = await probe_engine(
        resolved_path, args=list(payload.args), env=dict(payload.env),
    )
    return {
        "uci_name": uci_name,
        "option_schema": schema,
        "probe_error": probe_error,
    }
