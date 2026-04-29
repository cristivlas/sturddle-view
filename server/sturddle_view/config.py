from __future__ import annotations

import secrets
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"


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

    # Default time control for new human-vs-engine games (Settings dialog).
    tc_initial_seconds: float = 300.0
    tc_increment_seconds: float = 0.0
    # Side the human plays: "white", "black", or "random".
    human_side: str = "white"
    # Whether take-back is allowed during human-vs-engine play.
    allow_takeback: bool = True
