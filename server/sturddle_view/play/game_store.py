"""Persistent snapshot of the in-progress human-vs-engine game.

Saved at every meaningful state change (move, pause/resume, take-back,
new game) so a server restart can rehydrate the position, clocks, and
turn without losing progress. Cleared on game-over.

Wall-clock think-time spent during a server outage is *not* charged: we
restore from the last save, so whichever side was on the clock effectively
gets a fresh think on whatever they had at save time.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_config_dir

from .. import APP_NAME
from .._atomic import atomic_write_json

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def default_state_path() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "current_game.json"


@dataclass
class GameState:
    game_id: str
    human_white: bool
    tc_initial_seconds: float
    tc_increment_seconds: float
    white_time: float
    black_time: float
    paused: bool
    moves_uci: list[str] = field(default_factory=list)
    # Per-ply (white_time, black_time) snapshots taken BEFORE the move at
    # that ply. Mirrors HumanVsEngine._clock_history so take-back still
    # restores prior clocks after a server restart.
    clock_history: list[list[float]] = field(default_factory=list)
    # Per-ply post-move engine eval, white POV. None for plies with no
    # engine search (human moves). Matches moves_uci length.
    eval_history: list[dict | None] = field(default_factory=list)
    # FEN of the board BEFORE any moves_uci were played. None means the
    # game began at the standard starting position. Required so restore_from
    # can rebuild a board imported from a non-startpos FEN/PGN.
    start_fen: str | None = None
    # Wall-clock seconds since epoch when the game was created. Persisted so
    # the autosaved PGN keeps the same filename after a server restart.
    game_started_wall: float | None = None
    version: int = SCHEMA_VERSION


class GameStore:
    """Atomic JSON-on-disk holder for the current game snapshot."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_state_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> GameState | None:
        if not self._path.exists():
            return None
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            log.exception("could not read %s; ignoring saved game", self._path)
            return None
        if data.get("version") != SCHEMA_VERSION:
            log.warning(
                "saved game schema mismatch (got %r, want %d); ignoring",
                data.get("version"), SCHEMA_VERSION,
            )
            return None
        try:
            return GameState(
                game_id=data["game_id"],
                human_white=bool(data["human_white"]),
                tc_initial_seconds=float(data["tc_initial_seconds"]),
                tc_increment_seconds=float(data["tc_increment_seconds"]),
                white_time=float(data["white_time"]),
                black_time=float(data["black_time"]),
                paused=bool(data.get("paused", False)),
                moves_uci=list(data.get("moves_uci", [])),
                clock_history=[list(p) for p in data.get("clock_history", [])],
                eval_history=list(data.get("eval_history", [])),
                start_fen=data.get("start_fen"),
                game_started_wall=data.get("game_started_wall"),
            )
        except (KeyError, TypeError, ValueError):
            log.exception("malformed saved game in %s; ignoring", self._path)
            return None

    def save(self, state: GameState) -> None:
        atomic_write_json(self._path, asdict(state))

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("could not delete %s", self._path)
