from __future__ import annotations

import logging
from pathlib import Path

import psutil
from fastapi import APIRouter, Depends, HTTPException, Request, status

from .. import __author__, __copyright__, __version__
from ..auth import require_token
from ..config import (
    HVE_DIFFICULTY_MAX,
    HVE_DIFFICULTY_MIN,
    VALID_AI_PROVIDERS,
    VALID_BOARD_STYLES,
    VALID_BOOK_ORDERS,
    VALID_EVAL_POVS,
    VALID_HUMAN_SIDES,
    VALID_RIBBON_SIDES,
)
from ..engines import EngineNotFoundError
from ..play.human_vs_engine import live_hve
from ._ai_kick import require_ai_provider_factory
from ._http import bad_request

log = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_token)])

# Sentinel echoed to the UI when an API key is set. UI never sees the
# real key back; user "Update"s by sending a new value.
_AI_KEY_MASK = "***"
# Wire field names. Named constants per project rule.
_PGN_AUTOSAVE_KEY = "pgn_autosave"
_PGN_DIR_KEY = "pgn_dir"
_TC_INITIAL_KEY = "tc_initial_seconds"
_TC_INCREMENT_KEY = "tc_increment_seconds"
_HUMAN_SIDE_KEY = "human_side"
_PLAYER_NAME_KEY = "player_name"
_ALLOW_TAKEBACK_KEY = "allow_takeback"
_AUTO_CLAIM_DRAWS_KEY = "auto_claim_draws"
_INHERIT_PGN_CLOCKS_KEY = "inherit_pgn_clocks"
_BOARD_STYLE_KEY = "board_style"
_PLAY_EVAL_POV_KEY = "play_eval_pov"
_PLAY_SHOW_EVAL_GRAPH_KEY = "play_show_eval_graph"
_VIEW_SHOW_PGN_COMMENTS_KEY = "view_show_pgn_comments"
_RIBBON_SIDE_KEY = "ribbon_side"
_ENGINE_THREADS_KEY = "engine_default_threads"
_ENGINE_ANALYSIS_THREADS_KEY = "engine_default_analysis_threads"
_ENGINE_HASH_MB_KEY = "engine_default_hash_mb"
_ENGINE_SYZYGY_PATH_KEY = "engine_default_syzygy_path"
_ENGINE_BOOK_PATH_KEY = "engine_default_book_path"
_ENGINE_BOOK_PLIES_KEY = "engine_default_book_plies"
_ENGINE_BOOK_ORDER_KEY = "engine_default_book_order"
_HVE_USE_OPENING_BOOK_KEY = "hve_use_opening_book"
_HVE_DIFFICULTY_KEY = "hve_difficulty"
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
_HOST_KEY = "host"
_LOGICAL_CORES_KEY = "logical_cores"
_PHYSICAL_CORES_KEY = "physical_cores"
_VERSION_KEY = "version"
_AUTHOR_KEY = "author"
_COPYRIGHT_KEY = "copyright"
# 100ms floor -- UCI wire is integer ms, and anything shorter is unplayable.
_TC_INITIAL_MIN = 0.1
_TC_INCREMENT_MIN = 0
# Mirrors the Settings dialog input's maxlength.
_PLAYER_NAME_MAX_LEN = 32
# Engine option overrides (threads, hash MB, book plies) need at least one.
_ENGINE_OPTION_MIN = 1
# Round caps must leave room for at least one full round.
_AI_ROUNDS_MIN = 1
# Depth caps must be at least one ply.
_AI_DEPTH_MIN = 1
# Anthropic requires budget_tokens >= 1024; same floor used here for both
# providers since 0/tiny budgets defeat the feature.
_AI_THINKING_BUDGET_MIN = 1024
# Scratch file written then removed to prove pgn_dir is writable.
_WRITE_PROBE_NAME = ".sv-write-probe"


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
        _PLAYER_NAME_KEY: s.player_name,
        _ALLOW_TAKEBACK_KEY: s.allow_takeback,
        _AUTO_CLAIM_DRAWS_KEY: s.auto_claim_draws,
        _INHERIT_PGN_CLOCKS_KEY: s.inherit_pgn_clocks,
        _BOARD_STYLE_KEY: s.board_style,
        _PLAY_EVAL_POV_KEY: s.play_eval_pov,
        _PLAY_SHOW_EVAL_GRAPH_KEY: s.play_show_eval_graph,
        _VIEW_SHOW_PGN_COMMENTS_KEY: s.view_show_pgn_comments,
        _RIBBON_SIDE_KEY: s.ribbon_side,
        _ENGINE_THREADS_KEY: s.engine_default_threads,
        _ENGINE_ANALYSIS_THREADS_KEY: s.engine_default_analysis_threads,
        _ENGINE_HASH_MB_KEY: s.engine_default_hash_mb,
        _ENGINE_SYZYGY_PATH_KEY: s.engine_default_syzygy_path,
        _ENGINE_BOOK_PATH_KEY: s.engine_default_book_path,
        _ENGINE_BOOK_PLIES_KEY: s.engine_default_book_plies,
        _ENGINE_BOOK_ORDER_KEY: s.engine_default_book_order,
        _HVE_USE_OPENING_BOOK_KEY: s.hve_use_opening_book,
        _HVE_DIFFICULTY_KEY: s.hve_difficulty,
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
        _HOST_KEY: {_LOGICAL_CORES_KEY: logical, _PHYSICAL_CORES_KEY: physical},
        _VERSION_KEY: __version__,
        _AUTHOR_KEY: __author__,
        _COPYRIGHT_KEY: __copyright__,
    }


@router.get("")
def get_settings(request: Request) -> dict:
    return _serialize(request.app.state.settings)


_LIVE_ENGINE_KEYS = (
    _ENGINE_THREADS_KEY,
    _ENGINE_HASH_MB_KEY,
    _ENGINE_SYZYGY_PATH_KEY,
)


# --- Validation helpers ---------------------------------------------------

def _is_blank(raw) -> bool:
    return raw is None or raw == ""


def _optional_str(raw) -> str | None:
    """Stripped string; blank or None -> None."""
    return (str(raw).strip() or None) if raw is not None else None


def _check_enum(key: str, value, valid: set[str]) -> None:
    if value not in valid:
        raise bad_request(f"{key} must be one of {sorted(valid)}")


def _check_min(key: str, value, min_value) -> None:
    if value < min_value:
        raise bad_request(f"{key} must be >= {min_value}")


def _parse_int(key: str, raw) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as e:
        raise bad_request(f"{key} must be an integer") from e


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
        _check_enum(key, v, valid)
        setattr(s, key, v)
    return apply


def _optional_enum_field(key: str, valid: set[str]):
    """Enum-or-clear: blank/None stores None; any other value must be in
    the valid set. For optional engine overrides like book order."""
    def apply(payload, s, request):
        raw = payload[key]
        if _is_blank(raw):
            setattr(s, key, None)
            return
        _check_enum(key, raw, valid)
        setattr(s, key, raw)
    return apply


def _float_field(key: str, *, min_value: float):
    def apply(payload, s, request):
        try:
            v = float(payload[key])
        except (TypeError, ValueError) as e:
            raise bad_request(f"{key} must be a number") from e
        _check_min(key, v, min_value)
        setattr(s, key, v)
    return apply


def _int_field(key: str, *, min_value: int, max_value: int | None = None):
    """Required-int field: reject non-integers and out-of-range values. Used
    for caps that always have a value (no clear-to-None semantics)."""
    def apply(payload, s, request):
        v = _parse_int(key, payload[key])
        _check_min(key, v, min_value)
        if max_value is not None and v > max_value:
            raise bad_request(f"{key} must be <= {max_value}")
        setattr(s, key, v)
    return apply


def _optional_int_field(key: str, *, min_value: int):
    """Override-or-clear int: blank/0 stores None ("no override"); any
    other value must be an integer >= min_value."""
    def apply(payload, s, request):
        raw = payload[key]
        if _is_blank(raw):
            setattr(s, key, None)
            return
        v = _parse_int(key, raw)
        if v == 0:
            setattr(s, key, None)
            return
        _check_min(key, v, min_value)
        setattr(s, key, v)
    return apply


def _optional_str_field(key: str):
    """Stripped string, blank stores None."""
    def apply(payload, s, request):
        setattr(s, key, _optional_str(payload[key]))
    return apply


def _str_field(key: str):
    """Stripped string, blank stores ''."""
    def apply(payload, s, request):
        setattr(s, key, str(payload[key] or "").strip())
    return apply


def _apply_player_name(payload, s, request):
    # Trim + cap to the UI input's maxlength; blank clears to "unset"
    # (game start falls back to the stock default).
    s.player_name = str(payload[_PLAYER_NAME_KEY] or "").strip()[:_PLAYER_NAME_MAX_LEN]


def _apply_pgn_dir(payload, s, request):
    raw = payload[_PGN_DIR_KEY]
    if not raw:
        s.pgn_dir = None
        return
    p = Path(raw).expanduser()
    if not p.is_dir():
        raise bad_request(f"{_PGN_DIR_KEY} does not exist or is not a directory: {p}")
    probe = p / _WRITE_PROBE_NAME
    try:
        probe.write_text("")
        probe.unlink()
    except OSError as e:
        raise bad_request(f"{_PGN_DIR_KEY} is not writable: {e}") from e
    s.pgn_dir = p


def _apply_ai_api_key(payload, s, request):
    # Not in PERSISTED_FIELDS: the ai_api_key setter stores it in the OS
    # keyring. The mask sentinel echoed by GET means "no change".
    raw = payload[_AI_API_KEY_KEY]
    if raw != _AI_KEY_MASK:
        s.ai_api_key = str(raw or "").strip()


def _apply_book_path(payload, s, request):
    # Reset the sequential cursor when the book changes -- a fresh book has
    # different lines, so the old offset is meaningless.
    new_path = _optional_str(payload[_ENGINE_BOOK_PATH_KEY])
    if new_path != s.engine_default_book_path:
        s.engine_default_book_cursor = 0
    s.engine_default_book_path = new_path


def _apply_analysis_engine(payload, s, request):
    # Empty clears the pin (analysis falls back to the active HvE engine).
    # A non-empty id must resolve in the registry, else the pin would
    # silently dangle and analysis would surprise the user later.
    raw = str(payload[_ANALYSIS_ENGINE_KEY] or "").strip()
    if raw:
        try:
            request.app.state.engines.get(raw)
        except EngineNotFoundError as e:
            raise bad_request(
                f"{_ANALYSIS_ENGINE_KEY}: engine '{raw}' not found in registry"
            ) from e
    s.analysis_engine_id = raw


# Payload key -> applier. Insertion order is application order; it mirrors
# the legacy cascade so any order-sensitive behavior is preserved.
_APPLIERS = {
    _PGN_AUTOSAVE_KEY: _bool_field(_PGN_AUTOSAVE_KEY),
    _PGN_DIR_KEY: _apply_pgn_dir,
    _TC_INITIAL_KEY: _float_field(_TC_INITIAL_KEY, min_value=_TC_INITIAL_MIN),
    _TC_INCREMENT_KEY: _float_field(_TC_INCREMENT_KEY, min_value=_TC_INCREMENT_MIN),
    _HUMAN_SIDE_KEY: _enum_field(_HUMAN_SIDE_KEY, VALID_HUMAN_SIDES),
    _PLAYER_NAME_KEY: _apply_player_name,
    _ALLOW_TAKEBACK_KEY: _bool_field(_ALLOW_TAKEBACK_KEY),
    _AUTO_CLAIM_DRAWS_KEY: _bool_field(_AUTO_CLAIM_DRAWS_KEY),
    _INHERIT_PGN_CLOCKS_KEY: _bool_field(_INHERIT_PGN_CLOCKS_KEY),
    _BOARD_STYLE_KEY: _enum_field(_BOARD_STYLE_KEY, VALID_BOARD_STYLES),
    _PLAY_EVAL_POV_KEY: _enum_field(_PLAY_EVAL_POV_KEY, VALID_EVAL_POVS),
    _PLAY_SHOW_EVAL_GRAPH_KEY: _bool_field(_PLAY_SHOW_EVAL_GRAPH_KEY),
    _VIEW_SHOW_PGN_COMMENTS_KEY: _bool_field(_VIEW_SHOW_PGN_COMMENTS_KEY),
    _RIBBON_SIDE_KEY: _enum_field(_RIBBON_SIDE_KEY, VALID_RIBBON_SIDES),
    _ENGINE_THREADS_KEY: _optional_int_field(_ENGINE_THREADS_KEY, min_value=_ENGINE_OPTION_MIN),
    _ENGINE_ANALYSIS_THREADS_KEY: _optional_int_field(
        _ENGINE_ANALYSIS_THREADS_KEY, min_value=_ENGINE_OPTION_MIN,
    ),
    _ENGINE_HASH_MB_KEY: _optional_int_field(_ENGINE_HASH_MB_KEY, min_value=_ENGINE_OPTION_MIN),
    _ENGINE_BOOK_PLIES_KEY: _optional_int_field(
        _ENGINE_BOOK_PLIES_KEY, min_value=_ENGINE_OPTION_MIN,
    ),
    _ENGINE_SYZYGY_PATH_KEY: _optional_str_field(_ENGINE_SYZYGY_PATH_KEY),
    _ENGINE_BOOK_PATH_KEY: _apply_book_path,
    _ENGINE_BOOK_ORDER_KEY: _optional_enum_field(_ENGINE_BOOK_ORDER_KEY, VALID_BOOK_ORDERS),
    _HVE_USE_OPENING_BOOK_KEY: _bool_field(_HVE_USE_OPENING_BOOK_KEY),
    _HVE_DIFFICULTY_KEY: _int_field(
        _HVE_DIFFICULTY_KEY, min_value=HVE_DIFFICULTY_MIN, max_value=HVE_DIFFICULTY_MAX,
    ),
    _AI_ENABLED_KEY: _bool_field(_AI_ENABLED_KEY),
    _AI_PROVIDER_KEY: _enum_field(_AI_PROVIDER_KEY, VALID_AI_PROVIDERS),
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
        log.error("failed to persist settings", exc_info=True)

    # Live-apply: only globals that flow into _spawn_engine's option layering
    # (Threads/Hash/SyzygyPath). Other fields (PGN, eval POV, board style)
    # are read at use time and don't need an engine respawn.
    if any(k in payload for k in _LIVE_ENGINE_KEYS):
        hve = live_hve(request.app.state)
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
    factory = require_ai_provider_factory(request)
    try:
        provider = factory()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"provider build failed: {e}",
        ) from e
    try:
        models = await provider.list_models()
    except NotImplementedError as e:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)) from e
    # model id -> "adaptive" | "extended" | "none"; {} for providers
    # without distinct thinking wire shapes. Drives the budget-tokens
    # field visibility in the Settings dialog.
    return {"models": models, "thinking": provider.thinking_modes(models)}
