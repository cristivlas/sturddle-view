"""Runner protocol -- abstract surface for tournament-manager backends.

The shipped impl is ``FastchessRunner`` in ``fastchess.py``; the protocol
exists so a future ``CutechessRunner`` can drop in without rewriting callers.
Web-agnostic -- events flow through an ``on_event`` callback supplied by the
orchestrator (routes to WebSocket or a local CLI consumer).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .store import Tournament


# An event carries a ``kind`` and an arbitrary ``payload`` dict.
EVT_STARTED = "started"            # runner just spawned the subprocess
EVT_DONE = "done"                  # runner exited cleanly (rounds completed)
EVT_STOPPED = "stopped"            # runner was killed via ``stop()``
EVT_RUNNER_CRASH = "runner_crash"  # non-zero rc not caused by ``stop()``
EVT_RUNNER_LOG = "runner_log"      # one line of runner output (chatty)
TERMINAL_EVENTS = frozenset({EVT_DONE, EVT_STOPPED, EVT_RUNNER_CRASH})

# Terminal-event payload keys: the exit code, plus the last stderr lines
# on a crash.
RC_KEY = "rc"
STDERR_TAIL_KEY = "stderr_tail"

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

    # When set, the runner wraps each engine in the proxy script so its
    # UCI traffic is broadcast to the GUI server. ``None`` disables the
    # wrap (used by tests that want raw fastchess argv).
    proxy_broadcast_url: str | None = None
    proxy_secret: str | None = None

    # Global engine defaults from settings, snapshot at start time.
    # Each None = no override; the runner skips the corresponding
    # fastchess flag. Replaces the legacy hash/threads/tablebase/book
    # fields the template used to carry.
    engine_default_threads: int | None = None
    engine_default_hash_mb: int | None = None
    engine_default_syzygy_path: str | None = None
    engine_default_book_path: str | None = None
    engine_default_book_plies: int | None = None
    engine_default_book_order: str | None = None  # "sequential" | "random" | None


class Runner(Protocol):
    async def start(self, spec: RunSpec, on_event: EventCallback) -> None: ...
    async def stop(self) -> None: ...
    def is_running(self) -> bool: ...
