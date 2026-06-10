from __future__ import annotations

import logging
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import __author__, __copyright__, __version__
from ..auth import require_token
from ..engines import EngineNotFoundError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_token)])

_VALID_SIDES = {"white", "black", "random"}
_VALID_EVAL_POV = {"white", "engine", "human"}
_VALID_RIBBON_SIDES = {"left", "right"}
# AI provider names -- single source of truth. Both the validation set
# here and app.py's provider-dispatch chain reference these, so adding a
# provider can't drift the two out of sync.
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_GEMINI = "gemini"
PROVIDER_OLLAMA = "ollama"
_VALID_AI_PROVIDERS = {PROVIDER_ANTHROPIC, PROVIDER_GEMINI, PROVIDER_OLLAMA}
# Sentinel echoed to the UI when an API key is set. UI never sees the
# real key back; user "Update"s by sending a new value.
_AI_KEY_MASK = "***"
# Wire field names. Named constants per project rule.
_PGN_AUTOSAVE_KEY = "pgn_autosave"
_PGN_DIR_KEY = "pgn_dir"
_TC_INITIAL_KEY = "tc_initial_seconds"
_TC_INCREMENT_KEY = "tc_increment_seconds"
_HUMAN_SIDE_KEY = "human_side"
_ALLOW_TAKEBACK_KEY = "allow_takeback"
_AUTO_CLAIM_DRAWS_KEY = "auto_claim_draws"
_INHERIT_PGN_CLOCKS_KEY = "inherit_pgn_clocks"
_BOARD_STYLE_KEY = "board_style"
_PLAY_EVAL_POV_KEY = "play_eval_pov"
_VIEW_SHOW_PGN_COMMENTS_KEY = "view_show_pgn_comments"
_RIBBON_SIDE_KEY = "ribbon_side"
_ENGINE_THREADS_KEY = "engine_default_threads"
_ENGINE_ANALYSIS_THREADS_KEY = "engine_default_analysis_threads"
_ENGINE_HASH_MB_KEY = "engine_default_hash_mb"
_ENGINE_SYZYGY_PATH_KEY = "engine_default_syzygy_path"
_ENGINE_BOOK_PATH_KEY = "engine_default_book_path"
_ENGINE_BOOK_PLIES_KEY = "engine_default_book_plies"
_ENGINE_BOOK_ORDER_KEY = "engine_default_book_order"
_AI_ENABLED_KEY = "ai_enabled"
_AI_PROVIDER_KEY = "ai_provider"
_AI_MODEL_KEY = "ai_model"
_AI_BASE_URL_KEY = "ai_base_url"
_AI_API_KEY_KEY = "ai_api_key"
_AI_API_KEY_SET_KEY = "ai_api_key_set"
_AI_THINKING_ENABLED_KEY = "ai_thinking_enabled"
_AI_THINKING_BUDGET_TOKENS_KEY = "ai_thinking_budget_tokens"
_AI_MAX_TOOL_ROUNDS_KEY = "ai_max_tool_rounds"
_AI_VERIFIER_MAX_ROUNDS_KEY = "ai_verifier_max_rounds"
_AI_ANALYZE_MAX_DEPTH_KEY = "ai_analyze_max_depth"
_AI_VERIFICATION_DEPTH_KEY = "ai_verification_depth"
_ANALYSIS_ENGINE_KEY = "analysis_engine_id"
# 100ms floor -- UCI wire is integer ms, and anything shorter is unplayable.
_TC_INITIAL_MIN = 0.1
# Round caps must leave room for at least one full round.
_AI_ROUNDS_MIN = 1
# Depth caps must be at least one ply.
_AI_DEPTH_MIN = 1
# Anthropic requires budget_tokens >= 1024; same floor used here for both
# providers since 0/tiny budgets defeat the feature.
_AI_THINKING_BUDGET_MIN = 1024
_VALID_BOARD_STYLES = {
    "classic", "classic-staunty",
    "green", "green-staunty",
    "blue", "chess-club",
    "black-and-white", "high-contrast",
    "sturddle-staunty", "sturddle-wine",
}
_BOOK_ORDER_SEQUENTIAL = "sequential"
_BOOK_ORDER_RANDOM = "random"
_VALID_BOOK_ORDERS = {_BOOK_ORDER_SEQUENTIAL, _BOOK_ORDER_RANDOM}


def _serialize(s) -> dict:
    # Host info bundled here so the settings dialog can cap thread inputs
    # without trusting navigator.hardwareConcurrency (which has been seen
    # lying in WebView2 / privacy-throttling browsers).
    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    return {
        _PGN_AUTOSAVE_KEY: s.pgn_autosave,
        _PGN_DIR_KEY: str(s.pgn_dir) if s.pgn_dir else "",
        _TC_INITIAL_KEY: s.tc_initial_seconds,
        _TC_INCREMENT_KEY: s.tc_increment_seconds,
        _HUMAN_SIDE_KEY: s.human_side,
        _ALLOW_TAKEBACK_KEY: s.allow_takeback,
        _AUTO_CLAIM_DRAWS_KEY: s.auto_claim_draws,
        _INHERIT_PGN_CLOCKS_KEY: s.inherit_pgn_clocks,
        _BOARD_STYLE_KEY: s.board_style,
        _PLAY_EVAL_POV_KEY: s.play_eval_pov,
        _VIEW_SHOW_PGN_COMMENTS_KEY: s.view_show_pgn_comments,
        _RIBBON_SIDE_KEY: s.ribbon_side,
        _ENGINE_THREADS_KEY: s.engine_default_threads,
        _ENGINE_ANALYSIS_THREADS_KEY: s.engine_default_analysis_threads,
        _ENGINE_HASH_MB_KEY: s.engine_default_hash_mb,
        _ENGINE_SYZYGY_PATH_KEY: s.engine_default_syzygy_path,
        _ENGINE_BOOK_PATH_KEY: s.engine_default_book_path,
        _ENGINE_BOOK_PLIES_KEY: s.engine_default_book_plies,
        _ENGINE_BOOK_ORDER_KEY: s.engine_default_book_order,
        _AI_ENABLED_KEY: s.ai_enabled,
        _AI_PROVIDER_KEY: s.ai_provider,
        _AI_MODEL_KEY: s.ai_model,
        _AI_BASE_URL_KEY: s.ai_base_url,
        _AI_API_KEY_SET_KEY: bool(s.ai_api_key),
        _AI_API_KEY_KEY: _AI_KEY_MASK if s.ai_api_key else "",
        _AI_THINKING_ENABLED_KEY: s.ai_thinking_enabled,
        _AI_THINKING_BUDGET_TOKENS_KEY: s.ai_thinking_budget_tokens,
        _AI_MAX_TOOL_ROUNDS_KEY: s.ai_max_tool_rounds,
        _AI_VERIFIER_MAX_ROUNDS_KEY: s.ai_verifier_max_rounds,
        _AI_ANALYZE_MAX_DEPTH_KEY: s.ai_analyze_max_depth,
        _AI_VERIFICATION_DEPTH_KEY: s.ai_verification_depth,
        _ANALYSIS_ENGINE_KEY: s.analysis_engine_id,
        "host": {"logical_cores": logical, "physical_cores": physical},
        "version": __version__,
        "author": __author__,
        "copyright": __copyright__,
    }


@router.get("")
def get_settings(request: Request) -> dict:
    return _serialize(request.app.state.settings)


_LIVE_ENGINE_KEYS = (
    _ENGINE_THREADS_KEY,
    _ENGINE_HASH_MB_KEY,
    _ENGINE_SYZYGY_PATH_KEY,
)


# --- Applier factories ----------------------------------------------------
#
# update_settings is a dispatch over these: each payload key maps to an
# applier `(payload, s, request) -> None` invoked only when the key is
# present. Wire key == Settings attr name for every dispatched field, so
# the factories take a single `key` and both look up and assign by it. Bad
# input raises HTTPException(400). Bespoke fields (pgn_dir, ai_api_key,
# analysis_engine_id) get hand-written appliers.

def _bool_field(key: str):
    def apply(payload, s, request):
        setattr(s, key, bool(payload[key]))
    return apply


def _enum_field(key: str, valid: set[str]):
    def apply(payload, s, request):
        v = payload[key]
        if v not in valid:
            raise HTTPException(
                status_code=400, detail=f"{key} must be one of {sorted(valid)}",
            )
        setattr(s, key, v)
    return apply


def _optional_enum_field(key: str, valid: set[str]):
    """Enum-or-clear: blank/None stores None; any other value must be in
    the valid set. For optional engine overrides like book order."""
    def apply(payload, s, request):
        raw = payload[key]
        if raw is None or raw == "":
            setattr(s, key, None)
            return
        if raw not in valid:
            raise HTTPException(
                status_code=400, detail=f"{key} must be one of {sorted(valid)}",
            )
        setattr(s, key, raw)
    return apply


def _float_field(key: str, *, min_value: float):
    def apply(payload, s, request):
        try:
            v = float(payload[key])
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"{key} must be a number") from e
        if v < min_value:
            raise HTTPException(status_code=400, detail=f"{key} must be >= {min_value}")
        setattr(s, key, v)
    return apply


def _int_field(key: str, *, min_value: int):
    """Required-int field: reject non-integers and sub-floor values. Used
    for caps that always have a value (no clear-to-None semantics)."""
    def apply(payload, s, request):
        try:
            v = int(payload[key])
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"{key} must be an integer") from e
        if v < min_value:
            raise HTTPException(status_code=400, detail=f"{key} must be >= {min_value}")
        setattr(s, key, v)
    return apply


def _optional_int_field(key: str, *, min_value: int):
    """Override-or-clear int: blank/0 stores None ("no override"); any
    other value must be an integer >= min_value."""
    def apply(payload, s, request):
        raw = payload[key]
        if raw is None or raw == "":
            setattr(s, key, None)
            return
        try:
            v = int(raw)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"{key} must be an integer") from e
        if v == 0:
            setattr(s, key, None)
            return
        if v < min_value:
            raise HTTPException(status_code=400, detail=f"{key} must be >= {min_value}")
        setattr(s, key, v)
    return apply


def _optional_str_field(key: str):
    """Stripped string, blank stores None."""
    def apply(payload, s, request):
        raw = payload[key]
        setattr(s, key, (str(raw).strip() or None) if raw is not None else None)
    return apply


def _str_field(key: str):
    """Stripped string, blank stores ''."""
    def apply(payload, s, request):
        setattr(s, key, str(payload[key] or "").strip())
    return apply


def _apply_pgn_dir(payload, s, request):
    raw = payload[_PGN_DIR_KEY]
    if not raw:
        s.pgn_dir = None
        return
    p = Path(raw).expanduser()
    if not p.is_dir():
        raise HTTPException(
            status_code=400, detail=f"{_PGN_DIR_KEY} does not exist or is not a directory: {p}",
        )
    probe = p / ".sv-write-probe"
    try:
        probe.write_text("")
        probe.unlink()
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"{_PGN_DIR_KEY} is not writable: {e}") from e
    s.pgn_dir = p


def _apply_ai_api_key(payload, s, request):
    # Session-only: not in PERSISTED_FIELDS. Server mode loads from
    # SV_AI_API_KEY env at startup; desktop will use OS keyring later.
    # The mask sentinel echoed by GET means "no change".
    raw = payload[_AI_API_KEY_KEY]
    if raw != _AI_KEY_MASK:
        s.ai_api_key = str(raw or "").strip()


def _apply_analysis_engine(payload, s, request):
    # Empty clears the pin (analysis falls back to the active HvE engine).
    # A non-empty id must resolve in the registry, else the pin would
    # silently dangle and analysis would surprise the user later.
    raw = str(payload[_ANALYSIS_ENGINE_KEY] or "").strip()
    if raw:
        try:
            request.app.state.engines.get(raw)
        except EngineNotFoundError as e:
            raise HTTPException(
                status_code=400,
                detail=f"{_ANALYSIS_ENGINE_KEY}: engine '{raw}' not found in registry",
            ) from e
    s.analysis_engine_id = raw


# Payload key -> applier. Insertion order is application order; it mirrors
# the legacy cascade so any order-sensitive behavior is preserved.
_APPLIERS = {
    _PGN_AUTOSAVE_KEY: _bool_field(_PGN_AUTOSAVE_KEY),
    _PGN_DIR_KEY: _apply_pgn_dir,
    _TC_INITIAL_KEY: _float_field(_TC_INITIAL_KEY, min_value=_TC_INITIAL_MIN),
    _TC_INCREMENT_KEY: _float_field(_TC_INCREMENT_KEY, min_value=0),
    _HUMAN_SIDE_KEY: _enum_field(_HUMAN_SIDE_KEY, _VALID_SIDES),
    _ALLOW_TAKEBACK_KEY: _bool_field(_ALLOW_TAKEBACK_KEY),
    _AUTO_CLAIM_DRAWS_KEY: _bool_field(_AUTO_CLAIM_DRAWS_KEY),
    _INHERIT_PGN_CLOCKS_KEY: _bool_field(_INHERIT_PGN_CLOCKS_KEY),
    _BOARD_STYLE_KEY: _enum_field(_BOARD_STYLE_KEY, _VALID_BOARD_STYLES),
    _PLAY_EVAL_POV_KEY: _enum_field(_PLAY_EVAL_POV_KEY, _VALID_EVAL_POV),
    _VIEW_SHOW_PGN_COMMENTS_KEY: _bool_field(_VIEW_SHOW_PGN_COMMENTS_KEY),
    _RIBBON_SIDE_KEY: _enum_field(_RIBBON_SIDE_KEY, _VALID_RIBBON_SIDES),
    _ENGINE_THREADS_KEY: _optional_int_field(_ENGINE_THREADS_KEY, min_value=1),
    _ENGINE_ANALYSIS_THREADS_KEY: _optional_int_field(_ENGINE_ANALYSIS_THREADS_KEY, min_value=1),
    _ENGINE_HASH_MB_KEY: _optional_int_field(_ENGINE_HASH_MB_KEY, min_value=1),
    _ENGINE_BOOK_PLIES_KEY: _optional_int_field(_ENGINE_BOOK_PLIES_KEY, min_value=1),
    _ENGINE_SYZYGY_PATH_KEY: _optional_str_field(_ENGINE_SYZYGY_PATH_KEY),
    _ENGINE_BOOK_PATH_KEY: _optional_str_field(_ENGINE_BOOK_PATH_KEY),
    _ENGINE_BOOK_ORDER_KEY: _optional_enum_field(_ENGINE_BOOK_ORDER_KEY, _VALID_BOOK_ORDERS),
    _AI_ENABLED_KEY: _bool_field(_AI_ENABLED_KEY),
    _AI_PROVIDER_KEY: _enum_field(_AI_PROVIDER_KEY, _VALID_AI_PROVIDERS),
    _AI_MODEL_KEY: _str_field(_AI_MODEL_KEY),
    _AI_BASE_URL_KEY: _str_field(_AI_BASE_URL_KEY),
    _AI_THINKING_ENABLED_KEY: _bool_field(_AI_THINKING_ENABLED_KEY),
    _AI_THINKING_BUDGET_TOKENS_KEY: _int_field(
        _AI_THINKING_BUDGET_TOKENS_KEY, min_value=_AI_THINKING_BUDGET_MIN,
    ),
    _AI_MAX_TOOL_ROUNDS_KEY: _int_field(_AI_MAX_TOOL_ROUNDS_KEY, min_value=_AI_ROUNDS_MIN),
    _AI_VERIFIER_MAX_ROUNDS_KEY: _int_field(_AI_VERIFIER_MAX_ROUNDS_KEY, min_value=_AI_ROUNDS_MIN),
    _AI_ANALYZE_MAX_DEPTH_KEY: _int_field(_AI_ANALYZE_MAX_DEPTH_KEY, min_value=_AI_DEPTH_MIN),
    _AI_VERIFICATION_DEPTH_KEY: _int_field(_AI_VERIFICATION_DEPTH_KEY, min_value=_AI_DEPTH_MIN),
    _ANALYSIS_ENGINE_KEY: _apply_analysis_engine,
    _AI_API_KEY_KEY: _apply_ai_api_key,
}


@router.put("")
async def update_settings(payload: dict, request: Request) -> dict:
    s = request.app.state.settings
    for key, apply in _APPLIERS.items():
        if key in payload:
            apply(payload, s, request)

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


@router.get("/ai/models")
async def list_ai_models(request: Request) -> dict:
    """Lists models available from the currently-selected provider.

    Used by the Settings dialog to populate the model dropdown so the
    user picks from a real list rather than typing an id. Errors flow
    through as HTTPException 5xx so the client can fall back to a
    free-text input + display the message.
    """
    factory = getattr(request.app.state, "ai_provider_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="AI provider factory not initialized")
    try:
        provider = factory()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"provider build failed: {e}") from e
    try:
        models = await provider.list_models()
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"models": models}
