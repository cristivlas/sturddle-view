from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_token

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_token)])

_VALID_SIDES = {"white", "black", "random"}


def _serialize(s) -> dict:
    return {
        "pgn_autosave": s.pgn_autosave,
        "pgn_dir": str(s.pgn_dir),
        "tc_initial_seconds": s.tc_initial_seconds,
        "tc_increment_seconds": s.tc_increment_seconds,
        "human_side": s.human_side,
        "allow_takeback": s.allow_takeback,
    }


@router.get("")
def get_settings(request: Request) -> dict:
    return _serialize(request.app.state.settings)


@router.put("")
def update_settings(payload: dict, request: Request) -> dict:
    s = request.app.state.settings

    if "pgn_autosave" in payload:
        s.pgn_autosave = bool(payload["pgn_autosave"])
    if "pgn_dir" in payload and payload["pgn_dir"]:
        s.pgn_dir = Path(payload["pgn_dir"])

    if "tc_initial_seconds" in payload:
        try:
            v = float(payload["tc_initial_seconds"])
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="tc_initial_seconds must be a number") from e
        # 100ms floor — UCI wire is integer ms, and anything shorter is unplayable.
        if v < 0.1:
            raise HTTPException(status_code=400, detail="tc_initial_seconds must be >= 0.1")
        s.tc_initial_seconds = v

    if "tc_increment_seconds" in payload:
        try:
            v = float(payload["tc_increment_seconds"])
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail="tc_increment_seconds must be a number") from e
        if v < 0:
            raise HTTPException(status_code=400, detail="tc_increment_seconds must be >= 0")
        s.tc_increment_seconds = v

    if "human_side" in payload:
        side = payload["human_side"]
        if side not in _VALID_SIDES:
            raise HTTPException(
                status_code=400, detail=f"human_side must be one of {sorted(_VALID_SIDES)}"
            )
        s.human_side = side

    if "allow_takeback" in payload:
        s.allow_takeback = bool(payload["allow_takeback"])

    try:
        s.save_persisted()
    except OSError:
        pass

    return _serialize(s)
