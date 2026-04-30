"""Runner protocol — abstract surface for tournament-manager backends.

The Phase 1 implementation is ``FastchessRunner`` in ``fastchess.py``. The
abstraction exists so a ``CutechessRunner`` can be added later without
rewriting callers; it does not exist as a generalized facade speculating
on capabilities of unknown future runners.

The protocol is web-agnostic — events flow through an ``on_event``
callback the orchestrator supplies, which it can route to the WebSocket
layer or a local CLI consumer.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .store import Tournament


# An event carries a ``kind`` and an arbitrary ``payload`` dict. Known kinds:
#   "started"        — runner just spawned the subprocess
#   "done"           — runner exited cleanly (rounds completed)
#   "stopped"        — runner was killed via ``stop()``
#   "runner_crash"   — runner exited with non-zero rc not caused by ``stop()``
#   "log"            — a single line of runner output (advisory; not all callers
#                      will subscribe — chatty)
EventCallback = Callable[[str, dict], Awaitable[None]]


@dataclass
class RunSpec:
    """Everything a Runner needs to spawn a tournament. Built by the
    orchestrator from a ``Tournament`` plus the system-level fastchess
    binary path. Web-agnostic; the CLI wrapper builds the same struct.
    """
    tournament: Tournament
    binary_path: str
    work_dir: Path  # The tournament's <id>/ directory
    pgn_path: Path  # work_dir / "games.pgn"
    config_path: Path  # work_dir / "config.json"
    log_path: Path  # work_dir / "logs" / "fastchess.log"


class Runner(Protocol):
    async def start(self, spec: RunSpec, on_event: EventCallback) -> None: ...
    async def stop(self) -> None: ...
    def is_running(self) -> bool: ...
