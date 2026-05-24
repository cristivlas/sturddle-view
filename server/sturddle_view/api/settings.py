from __future__ import annotations

from pathlib import Path

import psutil
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import __author__, __copyright__, __version__
from ..auth import require_token

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_token)])

_VALID_SIDES = {"white", "black", "random"}
_VALID_EVAL_POV = {"white", "engine", "human"}
_VALID_RIBBON_SIDES = {"left", "right"}
_VALID_AI_PROVIDERS = {"anthropic", "ollama"}
# Sentinel echoed to the UI when an API key is set. UI never sees the
# real key back; user "Update"s by sending a new value.
_AI_KEY_MASK = "***"
_VALID_BOARD_STYLES = {
    "classic", "classic-staunty",
    "green", "green-staunty",
    "blue", "chess-club",
    "black-and-white", "high-contrast",
}


def _serialize(s) -> dict:
    # Host info bundled here so the settings dialog can cap thread inputs
    # without trusting navigator.hardwareConcurrency (which has been seen
    # lying in WebView2 / privacy-throttling browsers).
    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    return {
        "pgn_autosave": s.pgn_autosave,
        "pgn_dir": str(s.pgn_dir) if s.pgn_dir else "",
        "tc_initial_seconds": s.tc_initial_seconds,
        "tc_increment_seconds": s.tc_increment_seconds,
        "human_side": s.human_side,
        "allow_takeback": s.allow_takeback,
        "auto_claim_draws": s.auto_claim_draws,
        "inherit_pgn_clocks": s.inherit_pgn_clocks,
        "board_style": s.board_style,
        "play_eval_pov": s.play_eval_pov,
        "view_show_pgn_comments": s.view_show_pgn_comments,
        "ribbon_side": s.ribbon_side,
        "engine_default_threads": s.engine_default_threads,
        "engine_default_analysis_threads": s.engine_default_analysis_threads,
        "engine_default_hash_mb": s.engine_default_hash_mb,
        "engine_default_syzygy_path": s.engine_default_syzygy_path,
        "engine_default_book_path": s.engine_default_book_path,
        "engine_default_book_plies": s.engine_default_book_plies,
        "engine_default_book_order": s.engine_default_book_order,
        "ai_enabled": s.ai_enabled,
        "ai_provider": s.ai_provider,
        "ai_model": s.ai_model,
        "ai_base_url": s.ai_base_url,
        "ai_api_key_set": bool(s.ai_api_key),
        "ai_api_key": _AI_KEY_MASK if s.ai_api_key else "",
        "host": {"logical_cores": logical, "physical_cores": physical},
        "version": __version__,
        "author": __author__,
        "copyright": __copyright__,
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


_LIVE_ENGINE_KEYS = (
    "engine_default_threads",
    "engine_default_hash_mb",
    "engine_default_syzygy_path",
)


@router.put("")
async def update_settings(payload: dict, request: Request) -> dict:
    s = request.app.state.settings

    if "pgn_autosave" in payload:
        s.pgn_autosave = bool(payload["pgn_autosave"])
    if "pgn_dir" in payload:
        raw = payload["pgn_dir"]
        if raw:
            p = Path(raw).expanduser()
            if not p.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail=f"pgn_dir does not exist or is not a directory: {p}",
                )
            probe = p / ".sv-write-probe"
            try:
                probe.write_text("")
                probe.unlink()
            except OSError as e:
                raise HTTPException(
                    status_code=400, detail=f"pgn_dir is not writable: {e}",
                ) from e
            s.pgn_dir = p
        else:
            s.pgn_dir = None

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

    if "auto_claim_draws" in payload:
        s.auto_claim_draws = bool(payload["auto_claim_draws"])

    if "inherit_pgn_clocks" in payload:
        s.inherit_pgn_clocks = bool(payload["inherit_pgn_clocks"])

    if "board_style" in payload:
        style = payload["board_style"]
        if style not in _VALID_BOARD_STYLES:
            raise HTTPException(
                status_code=400,
                detail=f"board_style must be one of {sorted(_VALID_BOARD_STYLES)}",
            )
        s.board_style = style

    if "play_eval_pov" in payload:
        pov = payload["play_eval_pov"]
        if pov not in _VALID_EVAL_POV:
            raise HTTPException(
                status_code=400,
                detail=f"play_eval_pov must be one of {sorted(_VALID_EVAL_POV)}",
            )
        s.play_eval_pov = pov

    if "view_show_pgn_comments" in payload:
        s.view_show_pgn_comments = bool(payload["view_show_pgn_comments"])

    if "ribbon_side" in payload:
        side = payload["ribbon_side"]
        if side not in _VALID_RIBBON_SIDES:
            raise HTTPException(
                status_code=400,
                detail=f"ribbon_side must be one of {sorted(_VALID_RIBBON_SIDES)}",
            )
        s.ribbon_side = side

    for key, min_v in (
        ("engine_default_threads", 1),
        ("engine_default_analysis_threads", 1),
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

    if "ai_enabled" in payload:
        s.ai_enabled = bool(payload["ai_enabled"])

    if "ai_provider" in payload:
        provider = payload["ai_provider"]
        if provider not in _VALID_AI_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=f"ai_provider must be one of {sorted(_VALID_AI_PROVIDERS)}",
            )
        s.ai_provider = provider

    if "ai_model" in payload:
        s.ai_model = str(payload["ai_model"] or "").strip()

    if "ai_base_url" in payload:
        s.ai_base_url = str(payload["ai_base_url"] or "").strip()

    if "ai_api_key" in payload:
        # Session-only: not in PERSISTED_FIELDS. Server mode loads from
        # SV_AI_API_KEY env at startup; desktop will use OS keyring later.
        # The mask sentinel echoed by GET means "no change".
        raw = payload["ai_api_key"]
        if raw != _AI_KEY_MASK:
            s.ai_api_key = str(raw or "").strip()

    try:
        s.save_persisted()
    except OSError:
        pass

    # Live-apply: only globals that flow into _spawn_engine's option layering
    # (Threads/Hash/SyzygyPath). Other fields (PGN, eval POV, board style)
    # are read at use time and don't need an engine respawn.
    if any(k in payload for k in _LIVE_ENGINE_KEYS):
        hve = getattr(request.app.state, "hve", None)
        if hve is not None:
            await hve.apply_engine_settings_live()

    return _serialize(s)
