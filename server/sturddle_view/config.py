from __future__ import annotations

import json
import secrets
from pathlib import Path

from platformdirs import user_config_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ._atomic import atomic_write_json


REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"


def default_settings_file() -> Path:
    return Path(user_config_dir("sturddle-view")) / "settings.json"


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
    "tournament_fastchess_path",
    "tournament_root",
    "tournament_default_template",
)


class Settings(BaseSettings):
    """Process-level config. Read from env (STURDDLE_*) or .env at repo root."""

    model_config = SettingsConfigDict(
        env_prefix="STURDDLE_",
        env_file=str(REPO_ROOT / ".env"),
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8765
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(24))
    web_dir: Path = WEB_DIR
    pgn_autosave: bool = True
    pgn_dir: Path = REPO_ROOT / "pgn"
    engine_path: Path | None = None
    auth_disabled: bool = False

    tc_initial_seconds: float = 300.0
    tc_increment_seconds: float = 0.0
    human_side: str = "white"
    allow_takeback: bool = True

    # Tournament subsystem settings. None = use platform default / not configured.
    tournament_fastchess_path: str | None = None
    tournament_root: str | None = None
    tournament_default_template: dict = Field(default_factory=dict)

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
