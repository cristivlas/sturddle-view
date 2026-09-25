from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import app_config_dir
from . import key_store
from ._atomic import atomic_write_json
from ._runtime import app_root
from .chess.results import SIDE_BLACK, SIDE_WHITE
from .env_utils import env_path

log = logging.getLogger(__name__)


REPO_ROOT = app_root()
WEB_DIR = REPO_ROOT / "web"

# Default Anthropic thinking budget (tokens). Overridable via
# SV_AI_THINKING_BUDGET_TOKENS. 4096 is a moderate value above the API
# minimum (1024) -- enough headroom for tactical positions, small enough
# not to dominate the output-token cap.
_DEFAULT_AI_THINKING_BUDGET_TOKENS = 4096

# Agent-loop round caps (no-env defaults). ai_analysis imports these so the
# Settings field default and the module-level guardrail share one literal.
_DEFAULT_AI_MAX_TOOL_ROUNDS = 32
_DEFAULT_AI_VERIFIER_MAX_ROUNDS = 8

# Consecutive failed recommend_move calls (illegal/rejected) before the loop
# force-nudges the model to rank candidates with top_moves instead of guessing
# one move at a time. Env: SV_AI_MAX_RECOMMEND_FAILURES.
_DEFAULT_AI_MAX_RECOMMEND_FAILURES = 2

# Search-depth caps (no-env defaults). tools_engine imports these so the
# Settings field default and the tool-level clamp share one literal.
# ANALYZE_MAX_DEPTH clamps any tool's per-call depth; VERIFICATION_DEPTH is
# the end-of-turn recommend-verifier floor.
_DEFAULT_AI_ANALYZE_MAX_DEPTH = 30
_DEFAULT_AI_VERIFICATION_DEPTH = 25

# Default time control for new games (and the idle clock placeholder before
# any game starts). The Settings field defaults and HumanVsEngine share these
# literals; both bind to SV_TC_INITIAL_SECONDS / SV_TC_INCREMENT_SECONDS.
DEFAULT_TC_INITIAL_SECONDS = 300.0
DEFAULT_TC_INCREMENT_SECONDS = 0.0

# HvE difficulty bounds. MAX = full strength; below MAX the engine is
# blinded to part of its candidate pool -- see
# docs/hve-difficulty-spec.md. Shared by the settings API (validation),
# the UI (slider range), and the blinding path.
HVE_DIFFICULTY_MIN = 1
HVE_DIFFICULTY_MAX = 10

# Blinding tuning (no-env defaults). Per-candidate sweep time is
# max(floor, budget / legal moves): the budget spreads over big move
# lists, the floor keeps each score sane; the removal step converts
# (MAX - level) to the peak blinding probability; the clamp folds
# mates to a finite cp so the auto-ranged spread stays sane.
_DEFAULT_HVE_SWEEP_MOVETIME_SECONDS = 0.07
_DEFAULT_HVE_SWEEP_BUDGET_SECONDS = 1.0
_DEFAULT_HVE_REMOVAL_STEP = 0.10
_DEFAULT_HVE_SCORE_CLAMP_CP = 1000.0

# Win-prob admission cap (all levels). Sweep cp maps to win probability
# via a logistic with this scale (empirical engine fit); a move is
# admitted only if its win-prob drop vs the best stays under the cap --
# tight near equality (~110cp), auto-loosens when already behind.
_DEFAULT_HVE_WINPROB_SCALE_CP = 180.0
_DEFAULT_HVE_WINPROB_DROP_CAP = 0.15

# Deficit relief: when the engine is behind, lift a fraction of the
# blinding -- relief = min(cap, gain * (0.5 - best win prob)) moves the
# effective level that fraction of the way toward MAX. The cap (< 1)
# keeps the engine short of full strength, so the set level still rules.
_DEFAULT_HVE_DEFICIT_RELIEF_GAIN = 2.0
_DEFAULT_HVE_DEFICIT_RELIEF_CAP = 0.6

# Opening-book line order. Shared by the settings API (validation), the
# HVE seed path, and opening_lines (selection). None = fastchess default
# (sequential).
BOOK_ORDER_SEQUENTIAL = "sequential"
BOOK_ORDER_RANDOM = "random"
VALID_BOOK_ORDERS = {BOOK_ORDER_SEQUENTIAL, BOOK_ORDER_RANDOM}

# AI provider names. Shared by the Settings default, the settings API
# (validation), app's provider dispatch, and each provider's wire identity.
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_GEMINI = "gemini"
PROVIDER_OLLAMA = "ollama"
VALID_AI_PROVIDERS = {PROVIDER_ANTHROPIC, PROVIDER_GEMINI, PROVIDER_OLLAMA}

# Enumerated UI settings. Shared by the Settings defaults, the settings API
# (validation), and the code acting on them.
HUMAN_SIDE_RANDOM = "random"
VALID_HUMAN_SIDES = {SIDE_WHITE, SIDE_BLACK, HUMAN_SIDE_RANDOM}
EVAL_POV_WHITE = SIDE_WHITE
EVAL_POV_ENGINE = "engine"
EVAL_POV_HUMAN = "human"
VALID_EVAL_POVS = {EVAL_POV_WHITE, EVAL_POV_ENGINE, EVAL_POV_HUMAN}
RIBBON_SIDE_LEFT = "left"
RIBBON_SIDE_RIGHT = "right"
VALID_RIBBON_SIDES = {RIBBON_SIDE_LEFT, RIBBON_SIDE_RIGHT}
BOARD_STYLE_BLACK_AND_WHITE = "black-and-white"
VALID_BOARD_STYLES = {
    "classic", "classic-staunty",
    "green", "green-staunty",
    "blue", "chess-club",
    BOARD_STYLE_BLACK_AND_WHITE, "high-contrast",
    "sturddle-staunty", "sturddle-wine",
}


_SETTINGS_FILENAME = "settings.json"


def default_settings_file() -> Path:
    """Path to persisted user settings.

    ``SV_SETTINGS_FILE`` overrides the default."""
    return env_path("SV_SETTINGS_FILE", app_config_dir() / _SETTINGS_FILENAME)


LOOPBACK_HOST = "127.0.0.1"
WILDCARD_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
ROOT_PATH = "/"
_TOKEN_BYTES = 24
_SETTINGS_JSON_INDENT = 2

# Settings reads each field from SV_<FIELD>. __main__ and desktop pass CLI
# overrides to Settings() (in this or a worker process) through these.
ENV_PREFIX = "SV_"
ENV_HOST = f"{ENV_PREFIX}HOST"
ENV_PORT = f"{ENV_PREFIX}PORT"
ENV_TOKEN = f"{ENV_PREFIX}TOKEN"
ENV_ENGINE_PATH = f"{ENV_PREFIX}ENGINE_PATH"
ENV_AUTH_DISABLED = f"{ENV_PREFIX}AUTH_DISABLED"
ENV_TLS_CERT = f"{ENV_PREFIX}TLS_CERT"
ENV_TLS_KEY = f"{ENV_PREFIX}TLS_KEY"


# Settings attribute names, for code that addresses a setting by name: the
# persisted-field list, the settings API's wire keys, getattr lookups.
PGN_AUTOSAVE_KEY = "pgn_autosave"
PGN_DIR_KEY = "pgn_dir"
ENGINE_PATH_KEY = "engine_path"
TC_INITIAL_KEY = "tc_initial_seconds"
TC_INCREMENT_KEY = "tc_increment_seconds"
HUMAN_SIDE_KEY = "human_side"
PLAYER_NAME_KEY = "player_name"
ALLOW_TAKEBACK_KEY = "allow_takeback"
AUTO_CLAIM_DRAWS_KEY = "auto_claim_draws"
INHERIT_PGN_CLOCKS_KEY = "inherit_pgn_clocks"
BOARD_STYLE_KEY = "board_style"
PLAY_EVAL_POV_KEY = "play_eval_pov"
PLAY_SHOW_EVAL_GRAPH_KEY = "play_show_eval_graph"
VIEW_SHOW_PGN_COMMENTS_KEY = "view_show_pgn_comments"
RIBBON_SIDE_KEY = "ribbon_side"
TOURNAMENT_FASTCHESS_PATH_KEY = "tournament_fastchess_path"
TOURNAMENT_ROOT_KEY = "tournament_root"
TOURNAMENT_DEFAULT_TEMPLATE_KEY = "tournament_default_template"
TOURNAMENT_SPRT_DEFAULTS_KEY = "tournament_sprt_defaults"
ENGINE_THREADS_KEY = "engine_default_threads"
ENGINE_ANALYSIS_THREADS_KEY = "engine_default_analysis_threads"
ENGINE_HASH_MB_KEY = "engine_default_hash_mb"
ENGINE_SYZYGY_PATH_KEY = "engine_default_syzygy_path"
ENGINE_BOOK_PATH_KEY = "engine_default_book_path"
ENGINE_BOOK_PLIES_KEY = "engine_default_book_plies"
ENGINE_BOOK_ORDER_KEY = "engine_default_book_order"
ENGINE_BOOK_CURSOR_KEY = "engine_default_book_cursor"
HVE_USE_OPENING_BOOK_KEY = "hve_use_opening_book"
HVE_DIFFICULTY_KEY = "hve_difficulty"
AI_ENABLED_KEY = "ai_enabled"
AI_PROVIDER_KEY = "ai_provider"
AI_MODELS_KEY = "ai_models"
AI_MODEL_KEY = "ai_model"
AI_BASE_URL_KEY = "ai_base_url"
AI_API_KEY_KEY = "ai_api_key"
AI_THINKING_ENABLED_KEY = "ai_thinking_enabled"
AI_THINKING_BUDGET_TOKENS_KEY = "ai_thinking_budget_tokens"
AI_MAX_TOOL_ROUNDS_KEY = "ai_max_tool_rounds"
AI_VERIFIER_MAX_ROUNDS_KEY = "ai_verifier_max_rounds"
AI_ANALYZE_MAX_DEPTH_KEY = "ai_analyze_max_depth"
AI_VERIFICATION_DEPTH_KEY = "ai_verification_depth"
ANALYSIS_ENGINE_KEY = "analysis_engine_id"

# Fields persisted to disk. Excludes secrets (token), bind config (host/port),
# auth_disabled (CLI flag), and web_dir (deployment).
PERSISTED_FIELDS = (
    PGN_AUTOSAVE_KEY,
    PGN_DIR_KEY,
    ENGINE_PATH_KEY,
    TC_INITIAL_KEY,
    TC_INCREMENT_KEY,
    HUMAN_SIDE_KEY,
    PLAYER_NAME_KEY,
    ALLOW_TAKEBACK_KEY,
    AUTO_CLAIM_DRAWS_KEY,
    INHERIT_PGN_CLOCKS_KEY,
    BOARD_STYLE_KEY,
    PLAY_EVAL_POV_KEY,
    PLAY_SHOW_EVAL_GRAPH_KEY,
    VIEW_SHOW_PGN_COMMENTS_KEY,
    RIBBON_SIDE_KEY,
    TOURNAMENT_FASTCHESS_PATH_KEY,
    TOURNAMENT_ROOT_KEY,
    TOURNAMENT_DEFAULT_TEMPLATE_KEY,
    TOURNAMENT_SPRT_DEFAULTS_KEY,
    ENGINE_THREADS_KEY,
    ENGINE_ANALYSIS_THREADS_KEY,
    ENGINE_HASH_MB_KEY,
    ENGINE_SYZYGY_PATH_KEY,
    ENGINE_BOOK_PATH_KEY,
    ENGINE_BOOK_PLIES_KEY,
    ENGINE_BOOK_ORDER_KEY,
    ENGINE_BOOK_CURSOR_KEY,
    HVE_USE_OPENING_BOOK_KEY,
    HVE_DIFFICULTY_KEY,
    AI_ENABLED_KEY,
    AI_PROVIDER_KEY,
    AI_MODELS_KEY,
    AI_BASE_URL_KEY,
    AI_THINKING_ENABLED_KEY,
    AI_THINKING_BUDGET_TOKENS_KEY,
    AI_MAX_TOOL_ROUNDS_KEY,
    AI_VERIFIER_MAX_ROUNDS_KEY,
    AI_ANALYZE_MAX_DEPTH_KEY,
    AI_VERIFICATION_DEPTH_KEY,
    ANALYSIS_ENGINE_KEY,
    # ai_api_key intentionally NOT persisted: it lives in the OS keyring
    # (SV_AI_API_KEY as fallback). The JSON settings file must never hold
    # the plaintext key.
)

# Survive a settings reset: the tournaments folder is a data location, and
# resetting it would hide past tournaments' standings and games.
RESET_KEPT_FIELDS = frozenset({TOURNAMENT_ROOT_KEY})


class Settings(BaseSettings):
    """Process-level config. Read from env (SV_*) or .env at repo root."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=str(REPO_ROOT / ".env"),
        extra="ignore",
    )

    host: str = LOOPBACK_HOST
    port: int = DEFAULT_PORT
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(_TOKEN_BYTES))
    # Enables /_test/* endpoints (HVE install/state) used by the e2e
    # test subprocess fixture. Never set in production.
    test_mode: bool = False
    web_dir: Path = WEB_DIR
    pgn_autosave: bool = False
    pgn_dir: Path | None = None
    engine_path: Path | None = None
    auth_disabled: bool = False
    # TLS: paths set via CLI (--cert/--key). Both must be present or both None.
    tls_cert: Path | None = None
    tls_key: Path | None = None
    # Connect-from-mobile QR: hand out the Tailscale address when its
    # interface is up (reachable off-LAN too) instead of the LAN one.
    prefer_tailscale: bool = True

    tc_initial_seconds: float = DEFAULT_TC_INITIAL_SECONDS
    tc_increment_seconds: float = DEFAULT_TC_INCREMENT_SECONDS
    human_side: str = SIDE_WHITE
    # HVE human display name, shared by all clients. Empty = unset; game
    # start falls back to game_store.DEFAULT_PLAYER_NAME.
    player_name: str = ""
    allow_takeback: bool = True
    auto_claim_draws: bool = True
    # Play From Here: when True, new game inherits live clock values from
    # the PGN cursor (study time pressure / repro engine behavior under the
    # exact remaining time). When False (default), live clocks reset to
    # current TC's initial.
    inherit_pgn_clocks: bool = False
    board_style: str = BOARD_STYLE_BLACK_AND_WHITE
    # HVE eval display POV: "white" (default), "engine" (raw UCI -- the
    # engine's POV), or "human" (flipped to the human's color).
    play_eval_pov: str = EVAL_POV_WHITE
    # Play mode: show the per-move engine eval graph in the side rail
    # (desktop viewports only). Hidden when False.
    play_show_eval_graph: bool = True
    # View mode: show sanitized PGN move commentary in the left column
    # (desktop viewports only). Hidden when False.
    view_show_pgn_comments: bool = True
    # Side of the screen the board ribbon docks to. Toast stack and side
    # rail mirror to match.
    ribbon_side: str = RIBBON_SIDE_LEFT

    # Tournament subsystem settings. None = use platform default / not configured.
    tournament_fastchess_path: str | None = None
    tournament_root: str | None = None
    tournament_default_template: dict = Field(default_factory=dict)
    tournament_sprt_defaults: dict = Field(default_factory=dict)

    # Global engine defaults. Layered on top of per-engine UCI options at
    # launch time (HVE + tournament). Blank/None = no override.
    # Threads / Hash / SyzygyPath are UCI setoptions; book_path + book_plies
    # feed fastchess CLI args and the HVE opening book (see below).
    engine_default_threads: int | None = None
    # Override Threads while in analysis (UCI go-infinite). None = use the
    # play-time Threads value. Applied to a dedicated analysis engine
    # process spawned for the duration of the search and quit on exit.
    engine_default_analysis_threads: int | None = None
    engine_default_hash_mb: int | None = None
    engine_default_syzygy_path: str | None = None
    engine_default_book_path: str | None = None
    engine_default_book_plies: int | None = None
    # "sequential" | "random". None = fastchess default (sequential).
    engine_default_book_order: str | None = None
    # HVE-only: use the opening book above. Tournaments always use it;
    # HVE opts in here (off by default -- no surprise seeded games).
    # EPD books seed the start position; PGN books feed the engine book
    # replies while the played moves prefix-match a book line.
    hve_use_opening_book: bool = False
    # Per-server sequential cursor: advances on each book HVE game
    # (order != random). EPD: walks positions (modulo line count); PGN:
    # rotates the matching-pool anchor. Reset when the book path changes.
    engine_default_book_cursor: int = 0

    # HvE difficulty (MIN..MAX). MAX = full strength; below MAX the
    # engine is blinded to part of its candidate pool and plays its
    # best visible move at full depth (docs/hve-difficulty-spec.md).
    # Tuning knobs bind to SV_HVE_REMOVAL_STEP etc. via the SV_ prefix.
    hve_difficulty: int = HVE_DIFFICULTY_MAX
    hve_sweep_movetime_seconds: float = _DEFAULT_HVE_SWEEP_MOVETIME_SECONDS
    hve_sweep_budget_seconds: float = _DEFAULT_HVE_SWEEP_BUDGET_SECONDS
    hve_removal_step: float = _DEFAULT_HVE_REMOVAL_STEP
    hve_score_clamp_cp: float = _DEFAULT_HVE_SCORE_CLAMP_CP
    hve_winprob_scale_cp: float = _DEFAULT_HVE_WINPROB_SCALE_CP
    hve_winprob_drop_cap: float = _DEFAULT_HVE_WINPROB_DROP_CAP
    hve_deficit_relief_gain: float = _DEFAULT_HVE_DEFICIT_RELIEF_GAIN
    hve_deficit_relief_cap: float = _DEFAULT_HVE_DEFICIT_RELIEF_CAP

    # AI analysis & commentary. Master toggle gates the engine+AI behavior
    # off the existing Analyze ribbon buttons; provider/model/base_url are
    # UI-managed strings. ai_api_key lives in the OS keyring (see the
    # property below). Empty = unset.
    ai_enabled: bool = False
    ai_provider: str = PROVIDER_ANTHROPIC
    # Per-provider model memory: keys are provider names, values are model
    # ids. The active model for the current provider is `ai_models.get(
    # ai_provider, "")`. Open dict so adding a provider doesn't bump the
    # schema -- new keys appear automatically.
    ai_models: dict[str, str] = Field(default_factory=dict)
    ai_base_url: str = ""
    # Extended thinking / native reasoning. When True, the provider asks
    # the model to think before answering. Anthropic uses the `thinking`
    # body field; Ollama switches to the native /api/chat endpoint with
    # think=true. budget_tokens applies to Anthropic's enabled mode.
    ai_thinking_enabled: bool = False
    ai_thinking_budget_tokens: int = _DEFAULT_AI_THINKING_BUDGET_TOKENS
    # Agent-loop round caps. Defaults mirror ai_analysis.MAX_TOOL_ROUNDS /
    # VERIFIER_MAX_ROUNDS; both bind to SV_AI_MAX_TOOL_ROUNDS /
    # SV_AI_VERIFIER_MAX_ROUNDS via the SV_ env prefix.
    ai_max_tool_rounds: int = _DEFAULT_AI_MAX_TOOL_ROUNDS
    ai_verifier_max_rounds: int = _DEFAULT_AI_VERIFIER_MAX_ROUNDS
    # Search-depth caps. Mirror tools_engine.MAX_DEPTH / VERIFICATION_DEPTH;
    # bind to SV_AI_ANALYZE_MAX_DEPTH / SV_AI_VERIFICATION_DEPTH via SV_.
    ai_analyze_max_depth: int = _DEFAULT_AI_ANALYZE_MAX_DEPTH
    ai_verification_depth: int = _DEFAULT_AI_VERIFICATION_DEPTH
    # Engine pinned for analysis (both engine-only and AI-driven tool calls).
    # Empty string means "use the active HvE engine".
    analysis_engine_id: str = ""

    @property
    def ai_model(self) -> str:
        return self.ai_models.get(self.ai_provider, "")

    @ai_model.setter
    def ai_model(self, value: str) -> None:
        self.ai_models = {**self.ai_models, self.ai_provider: value}

    # API keys live in the OS keyring (see key_store.py); never persisted
    # in the settings file. The property dispatches by current provider
    # so the provider factory just reads s.ai_api_key.
    @property
    def ai_api_key(self) -> str:
        return key_store.get_api_key(self.ai_provider)

    @ai_api_key.setter
    def ai_api_key(self, value: str) -> None:
        key_store.set_api_key(self.ai_provider, value or "")

    def apply_persisted(self, path: Path | None = None) -> None:
        path = path or default_settings_file()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("ignoring unreadable settings file %s", path, exc_info=True)
            return
        for k in PERSISTED_FIELDS:
            if k in data and data[k] is not None:
                try:
                    setattr(self, k, data[k])
                except Exception:
                    log.warning("ignoring invalid persisted setting %s", k, exc_info=True)

    def save_persisted(self, path: Path | None = None) -> None:
        path = path or default_settings_file()
        payload = {}
        for k in PERSISTED_FIELDS:
            v = getattr(self, k, None)
            payload[k] = str(v) if isinstance(v, Path) else v
        atomic_write_json(path, payload, indent=_SETTINGS_JSON_INDENT)

    def reset_persisted(self) -> None:
        """Restore persisted fields to defaults; a fresh instance keeps SV_ env overrides."""
        defaults = type(self)()
        for k in PERSISTED_FIELDS:
            if k not in RESET_KEPT_FIELDS:
                setattr(self, k, getattr(defaults, k))
