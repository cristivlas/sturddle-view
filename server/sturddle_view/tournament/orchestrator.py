"""Unified facade over fastchess / cutechess-cli.

Responsibilities:
  - Rewrite engine paths in tournament configs to point at stdio proxies.
  - Launch the tournament manager as a subprocess (or attach to a running one).
  - On attach: reconstruct state from existing PGN/log, then switch to live proxy stream.
  - Detect tournament-manager crashes and surface them on the event bus.

This module is intentionally a stub; concrete adapters land in `fastchess.py`
and `cutechess.py` once the proxy contract stabilizes.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Manager(str, Enum):
    FASTCHESS = "fastchess"
    CUTECHESS = "cutechess"


@dataclass
class TournamentConfig:
    manager: Manager
    config_path: Path
    rounds: int = 1
    concurrency: int = 1
    sprt: dict | None = None


class Orchestrator:
    def __init__(self) -> None:
        self._proc = None  # asyncio.subprocess.Process when implemented

    async def start(self, config: TournamentConfig) -> None:
        raise NotImplementedError

    async def attach(self, pid: int) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError
