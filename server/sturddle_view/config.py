from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from platformdirs import user_config_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import app_dir_name
from . import key_store
from ._atomic import atomic_write_json
from ._runtime import app_root


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


def default_settings_file() -> Path:
    """Path to persisted user settings.

    ``SV_SETTINGS_FILE`` overrides the default."""
    override = os.environ.get("SV_SETTINGS_FILE")
    if override:
        return Path(override)
    return Path(user_config_dir(app_dir_name(), appauthor=False)) / "settings.json"


LOOPBACK_HOST = "127.0.0.1"
WILDCARD_HOST = "0.0.0.0"
# Desktop mode listens on every interface so phones on the LAN can
# connect (see the About dialog QR code); token auth still applies.
DESKTOP_DEFAULT_HOST = WILDCARD_HOST


# Fields persisted to disk. Excludes secrets (token), bind config (host/port),
# auth_disabled (CLI flag), and web_dir (deployment).
PERSISTED_FIELDS = (
    "pgn_autosave",
    "pgn_dir",
    "engine_path",
    "tc_initial_seconds",
    "tc_increment_seconds",
    "human_side",
    "player_name",
    "allow_takeback",
    "auto_claim_draws",
    "inherit_pgn_clocks",
    "board_style",
    "play_eval_pov",
    "play_show_eval_graph",
    "view_show_pgn_comments",
    "ribbon_side",
    "tournament_fastchess_path",
    "tournament_root",
    "tournament_default_template",
    "tournament_sprt_defaults",
    "engine_default_threads",
    "engine_default_analysis_threads",
    "engine_default_hash_mb",
    "engine_default_syzygy_path",
    "engine_default_book_path",
    "engine_default_book_plies",
    "engine_default_book_order",
    "engine_default_book_cursor",
    "hve_use_opening_book",
    "hve_difficulty",
    "ai_enabled",
    "ai_provider",
    "ai_models",
    "ai_base_url",
    "ai_thinking_enabled",
    "ai_thinking_budget_tokens",
    "ai_max_tool_rounds",
    "ai_verifier_max_rounds",
    "ai_analyze_max_depth",
    "ai_verification_depth",
    "analysis_engine_id",
    # ai_api_key intentionally NOT persisted: server mode reads SV_AI_API_KEY
    # from env; desktop mode will switch to OS keyring (later cycle). The
    # JSON settings file must never hold the plaintext key.
)


class Settings(BaseSettings):
    """Process-level config. Read from env (SV_*) or .env at repo root."""

    model_config = SettingsConfigDict(
        env_prefix="SV_",
        env_file=str(REPO_ROOT / ".env"),
        extra="ignore",
    )

    host: str = LOOPBACK_HOST
    port: int = 8765
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(24))
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

    tc_initial_seconds: float = DEFAULT_TC_INITIAL_SECONDS
    tc_increment_seconds: float = DEFAULT_TC_INCREMENT_SECONDS
    human_side: str = "white"
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
    board_style: str = "black-and-white"
    # HVE eval display POV: "white" (default, status quo), "engine"
    # (raw UCI — engine's POV), or "human" (flipped to human's color).
    play_eval_pov: str = "white"
    # Play mode: show the per-move engine eval graph in the side rail
    # (desktop viewports only). Hidden when False.
    play_show_eval_graph: bool = True
    # View mode: show sanitized PGN move commentary in the left column
    # (desktop viewports only). Hidden when False.
    view_show_pgn_comments: bool = True
    # Side of the screen the board ribbon docks to. Toast stack and side
    # rail mirror to match.
    ribbon_side: str = "left"

    # Tournament subsystem settings. None = use platform default / not configured.
    tournament_fastchess_path: str | None = None
    tournament_root: str | None = None
    tournament_default_template: dict = Field(default_factory=dict)
    tournament_sprt_defaults: dict = Field(default_factory=dict)

    # Global engine defaults. Layered on top of per-engine UCI options at
    # launch time (HVE + tournament). Blank/None = no override.
    # Threads / Hash / SyzygyPath are UCI setoptions; book_path + book_plies
    # are fastchess CLI args (tournament only — see HVE follow-up note).
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
    # UI-managed strings. ai_api_key is server-mode only (SV_AI_API_KEY);
    # desktop builds will switch to keyring later. Empty = unset.
    ai_enabled: bool = False
    ai_provider: str = "anthropic"
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
            return
        for k in PERSISTED_FIELDS:
            if k in data and data[k] is not None:
                try:
                    setattr(self, k, data[k])
                except Exception:
                    pass

    def save_persisted(self, path: Path | None = None) -> None:
        path = path or default_settings_file()
        payload = {}
        for k in PERSISTED_FIELDS:
            v = getattr(self, k, None)
            payload[k] = str(v) if isinstance(v, Path) else v
        atomic_write_json(path, payload, indent=2)
