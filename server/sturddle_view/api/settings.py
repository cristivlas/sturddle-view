from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_token

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_token)])


@router.get("")
def get_settings(request: Request) -> dict:
    s = request.app.state.settings
    # Expose only fields safe to surface to a client; never echo the token back.
    return {
        "pgn_autosave": s.pgn_autosave,
        "pgn_dir": str(s.pgn_dir),
    }


@router.put("")
def update_settings(payload: dict, request: Request) -> dict:
    # TODO: persist to disk; for now, mutate in-memory.
    s = request.app.state.settings
    if "pgn_autosave" in payload:
        s.pgn_autosave = bool(payload["pgn_autosave"])
    return get_settings(request)
