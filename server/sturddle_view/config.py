from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from platformdirs import user_config_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import APP_NAME
from ._atomic import atomic_write_json
from ._runtime import app_root


REPO_ROOT = app_root()
WEB_DIR = REPO_ROOT / "web"


def default_settings_file() -> Path:
    """Path to persisted user settings.

    ``SV_SETTINGS_FILE`` overrides the default."""
    override = os.environ.get("SV_SETTINGS_FILE")
    if override:
        return Path(override)
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "settings.json"


# Fields persisted to disk. Excludes secrets (token), bind config (host/port),
# auth_disabled (CLI flag), and web_dir (deployment).
PERSISTED_FIELDS = (
    "pgn_autosave",
    "pgn_dir",
    "engine_path",
    "tc_initial_seconds",
    "tc_increment_seconds",
    "human_side",
    "allow_takeback",
    "auto_claim_draws",
    "inherit_pgn_clocks",
    "board_style",
    "play_eval_pov",
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
    "ai_enabled",
    "ai_provider",
    "ai_model",
    "ai_base_url",
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

    host: str = "127.0.0.1"
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

    tc_initial_seconds: float = 300.0
    tc_increment_seconds: float = 0.0
    human_side: str = "white"
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

    # AI analysis & commentary. Master toggle gates the engine+AI behavior
    # off the existing Analyze ribbon buttons; provider/model/base_url are
    # UI-managed strings. ai_api_key is server-mode only (SV_AI_API_KEY);
    # desktop builds will switch to keyring later. Empty = unset.
    ai_enabled: bool = False
    ai_provider: str = "anthropic"
    ai_model: str = ""
    ai_base_url: str = ""
    ai_api_key: str = ""

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
