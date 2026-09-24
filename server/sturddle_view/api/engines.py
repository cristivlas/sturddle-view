from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from .. import is_windows
from ..auth import require_token
from ..engines import (
    UNSET,
    DuplicateEngineError,
    Engine,
    EngineNotFoundError,
    EngineRegistry,
    InvalidLaunchProfileError,
    probe_engine,
    resolve_selected,
    validate_launch_profile,
)
from ..error_detail import MESSAGE_KEY
from ..play.human_vs_engine import live_hve
from ..tournament.store import STATUS_RUNNING, TournamentStore
from ._http import bad_request, conflict, not_found

_ENGINE_NOT_FOUND = "engine not found"
_ENGINE_PATH = "/{engine_id}"
_SELECTED_ID_KEY = "selected_id"
# Running-tournament lock entries: {name, status}.
_LOCK_NAME_KEY = "name"
_LOCK_STATUS_KEY = "status"
_ENGINE_REF_ID_KEY = "id"


def _validate_engine_path(raw: str) -> str:
    """Resolve and check an engine path; raise HTTPException if unusable.

    Returns the resolved string path.
    """
    if not raw or not raw.strip():
        raise bad_request("engine path is empty")
    p = Path(raw).expanduser()
    try:
        p = p.resolve(strict=False)
    except OSError as e:
        raise bad_request(f"bad path: {e}") from e
    if not p.exists():
        raise bad_request(f"path does not exist: {p}")
    if p.is_dir():
        raise bad_request(f"path is a directory, not a file: {p}")
    if not p.is_file():
        raise bad_request(f"path is not a regular file: {p}")
    if not is_windows() and not os.access(p, os.X_OK):
        raise bad_request(f"path is not executable: {p}")
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
    # Approximate logistic Elo (display-only; anchors ordo fits).
    rating: int | None = None


class EngineUpdate(BaseModel):
    name: str | None = None
    path: str | None = None
    options: dict[str, Any] | None = None
    # Dialog Save sends these so a Refresh-probed schema/uci_name (captured
    # with in-progress args/env) is persisted with the rest of the edits.
    option_schema: dict[str, Any] | None = None
    uci_name: str | None = None
    # True when `name` was derived (Reset restore): collide -> suffix, not 409.
    auto_suffix: bool = False
    args: list[str] | None = None
    env: dict[str, str] | None = None
    # None clears the rating; omitting the field leaves it untouched
    # (distinguished via model_fields_set).
    rating: int | None = None


class EngineProbe(BaseModel):
    """Trial-spawn payload -- does not touch the registry. Used by the
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
        raise bad_request(str(e)) from e


def _registry(request: Request) -> EngineRegistry:
    return request.app.state.engines


def _engine_locks(request: Request) -> dict[str, list[dict]]:
    """Map engine registry id -> list of {name, status} for running tournaments.

    Terminal states (stopped/failed/done) don't lock: restart is from scratch.
    """
    ts: TournamentStore = getattr(request.app.state, "tournament_store", None)
    if ts is None:
        return {}
    locks: dict[str, list[dict]] = {}
    for t in ts.list():
        if t.status != STATUS_RUNNING:
            continue
        for ref in t.engines or []:
            eng_id = (
                ref.get(_ENGINE_REF_ID_KEY) if isinstance(ref, dict)
                else getattr(ref, _ENGINE_REF_ID_KEY, None)
            )
            if eng_id:
                locks.setdefault(eng_id, []).append(
                    {_LOCK_NAME_KEY: t.name, _LOCK_STATUS_KEY: t.status}
                )
    return locks


def _check_engine_locked(engine_id: str, request: Request) -> None:
    """Raise 409 if the engine is referenced by a running tournament."""
    refs = _engine_locks(request).get(engine_id, [])
    if refs:
        names = ", ".join(f"{r[_LOCK_NAME_KEY]} ({r[_LOCK_STATUS_KEY]})" for r in refs)
        raise conflict(f"engine in use by: {names}")


def _serialize(e: Engine) -> dict:
    return asdict(e)


async def _ensure_schema(reg: EngineRegistry, e: Engine) -> Engine:
    """Lazily capture option schema + uci_name for entries that predate them.

    Skips engines already probed (non-empty schema, or uci_name set --
    optionless engines would otherwise re-spawn on every GET). Best-effort:
    a failed probe changes nothing and is retried next call.
    """
    if e.option_schema or e.uci_name is not None:
        return e
    uci_name, schema, err = await probe_engine(
        e.path, args=list(e.args or []), env=dict(e.env or {}),
    )
    if err:
        return e
    try:
        return reg.update(e.id, option_schema=schema, uci_name=uci_name)
    except EngineNotFoundError:
        return e


@router.get("")
async def list_engines(request: Request, probe: bool = True) -> dict:
    """``probe=false`` skips the lazy schema probe -- read-only consumers
    (e.g. the start-time drift check) must not pay an engine-spawn per
    unprobed/broken registry entry."""
    reg = _registry(request)
    out = []
    for e in reg.list():
        if probe:
            e = await _ensure_schema(reg, e)
        out.append(_serialize(e))
    return {
        "engines": out,
        _SELECTED_ID_KEY: reg.selected_id,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_engine(payload: EngineCreate, request: Request) -> dict:
    reg = _registry(request)
    resolved_path = _validate_engine_path(payload.path)
    _validate_launch(payload.args, payload.env)
    uci_name, schema, probe_error = await probe_engine(
        resolved_path, args=list(payload.args), env=dict(payload.env),
    )
    if probe_error:
        # Reject unprobeable engines outright -- a half-broken registry
        # entry would just trip the user up later when they try to use it.
        raise bad_request(probe_error)
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
            uci_name=uci_name,
            rating=payload.rating,
        )
    except DuplicateEngineError as exc:
        raise conflict(str(exc)) from exc
    # First engine added with nothing selected -> auto-select it. Saves the
    # user a redundant click; any subsequent add leaves the active engine
    # alone.
    if reg.selected_id is None:
        reg.select(e.id)
    return _serialize(e)


@router.get(_ENGINE_PATH)
def get_engine(engine_id: str, request: Request) -> dict:
    reg = _registry(request)
    try:
        e = reg.get(engine_id)
    except EngineNotFoundError as exc:
        raise not_found(_ENGINE_NOT_FOUND) from exc
    locks = _engine_locks(request)
    d = _serialize(e)
    d["locked"] = locks.get(engine_id, [])
    return d


@router.patch(_ENGINE_PATH)
async def update_engine(engine_id: str, payload: EngineUpdate, request: Request) -> dict:
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
            option_schema=payload.option_schema,
            uci_name=payload.uci_name,
            auto_suffix=payload.auto_suffix,
            args=payload.args,
            env=payload.env,
            rating=payload.rating if "rating" in payload.model_fields_set else UNSET,
        )
    except EngineNotFoundError as exc:
        raise not_found(_ENGINE_NOT_FOUND) from exc
    except DuplicateEngineError as exc:
        raise conflict(str(exc)) from exc
    # If the edited engine is the one currently driving HvE, push the new
    # launch profile to the live instance so the next move uses it.
    s = request.app.state
    hve = live_hve(s)
    if hve is not None and reg.selected_id == engine_id:
        hve.apply_launch(resolve_selected(s.engines, s.settings))
        await hve.apply_engine_settings_live()
    return _serialize(e)


@router.delete(_ENGINE_PATH, status_code=status.HTTP_204_NO_CONTENT)
def remove_engine(engine_id: str, request: Request) -> None:
    _check_engine_locked(engine_id, request)
    reg = _registry(request)
    try:
        reg.remove(engine_id)
    except EngineNotFoundError as exc:
        raise not_found(_ENGINE_NOT_FOUND) from exc


@router.post("/{engine_id}/select")
async def select_engine(engine_id: str, request: Request) -> dict:
    reg = _registry(request)
    try:
        reg.select(engine_id)
    except EngineNotFoundError as exc:
        raise not_found(_ENGINE_NOT_FOUND) from exc
    # Eager swap so mid-analysis (no /game/* call follows) takes effect now.
    s = request.app.state
    hve = live_hve(s)
    if hve is not None:
        launch = resolve_selected(s.engines, s.settings)
        if launch.path is not None and hve.engine_path != launch.path:
            await hve.swap_engine(launch.path)
            hve.apply_launch(launch)
    return {_SELECTED_ID_KEY: engine_id}


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
        raise not_found(_ENGINE_NOT_FOUND) from exc
    uci_name, schema, probe_error = await probe_engine(
        e.path, args=list(e.args or []), env=dict(e.env or {}),
    )
    if probe_error:
        detail = f"could not capture options from engine: {probe_error[MESSAGE_KEY]}"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    # An empty schema on a clean probe is legitimate (optionless engine):
    # persist it plus uci_name so the entry stops re-probing.
    try:
        e = reg.update(engine_id, option_schema=schema, uci_name=uci_name)
    except EngineNotFoundError as exc:
        raise not_found(_ENGINE_NOT_FOUND) from exc
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
