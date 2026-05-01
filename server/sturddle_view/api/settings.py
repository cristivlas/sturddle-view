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
        "engine_default_threads": s.engine_default_threads,
        "engine_default_hash_mb": s.engine_default_hash_mb,
        "engine_default_syzygy_path": s.engine_default_syzygy_path,
        "engine_default_book_path": s.engine_default_book_path,
        "engine_default_book_plies": s.engine_default_book_plies,
        "engine_default_book_order": s.engine_default_book_order,
    }


# Distinguishes "field absent from payload" (don't touch) from "field
# present with cleared value" (set to None). Returned by the coercion
# helpers below; call sites compare with `is _SENTINEL`.
_SENTINEL: object = object()


def _coerce_optional_int(
    payload: dict, key: str, *, min_value: int | None = None,
) -> int | None | object:
    """Treat missing/blank/zero as cleared (None). UI sends "" or 0 to
    mean "no override"; either is normalized to None for storage."""
    if key not in payload:
        return _SENTINEL
    raw = payload[key]
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"{key} must be an integer") from e
    if v == 0:
        return None
    if min_value is not None and v < min_value:
        raise HTTPException(status_code=400, detail=f"{key} must be >= {min_value}")
    return v


def _coerce_optional_str(payload: dict, key: str) -> str | None | object:
    if key not in payload:
        return _SENTINEL
    raw = payload[key]
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


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

    for key, min_v in (
        ("engine_default_threads", 1),
        ("engine_default_hash_mb", 1),
        ("engine_default_book_plies", 1),
    ):
        v = _coerce_optional_int(payload, key, min_value=min_v)
        if v is not _SENTINEL:
            setattr(s, key, v)
    for key in ("engine_default_syzygy_path", "engine_default_book_path"):
        v = _coerce_optional_str(payload, key)
        if v is not _SENTINEL:
            setattr(s, key, v)
    if "engine_default_book_order" in payload:
        raw = payload["engine_default_book_order"]
        if raw is None or raw == "":
            s.engine_default_book_order = None
        elif raw in ("sequential", "random"):
            s.engine_default_book_order = raw
        else:
            raise HTTPException(
                status_code=400,
                detail="engine_default_book_order must be 'sequential' or 'random'",
            )

    try:
        s.save_persisted()
    except OSError:
        pass

    return _serialize(s)
