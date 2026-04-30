"""``FastchessRunner`` — implements the ``Runner`` protocol over fastchess.

Responsibilities (per ``docs/tournament-spec.md``):

  - Build the fastchess CLI from a frozen template.
  - Spawn with cross-platform process-group isolation
    (``CREATE_NEW_PROCESS_GROUP`` on Windows, ``start_new_session`` on Unix).
  - Drain stdout/stderr to ``logs/fastchess.log`` via background tasks.
  - ``stop()`` is a hard kill (``proc.kill()`` + ``proc.wait()``); idempotent.
  - Detect clean exit vs killed; emit ``done`` or ``stopped`` accordingly.

Cross-platform tactics cribbed from
``~/Projects/sturddle-2/tools/tuneup/spsa/worker.py``: process-group
isolation so the kill is targeted, pipe drain tasks so a full pipe
never deadlocks the wrapper.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from typing import Any

from .runner import EventCallback, RunSpec


log = logging.getLogger(__name__)


def build_command(spec: RunSpec) -> list[str]:
    """Translate a ``RunSpec`` into a fastchess argv.

    Template fields recognized (see ``docs/tournament-spec.md``):
      - tc                : str, fastchess tc= format ("10+0.1", "40/60", ...)
      - hash              : int (MB)
      - threads           : int
      - ponder            : bool
      - games_in_parallel : int  (fastchess -concurrency)
      - book              : str  path to opening book (epd or pgn)
      - book_format       : "epd" | "pgn"  (default "epd")
      - tablebase         : str  Syzygy path (per-engine SyzygyPath)
      - tournament_type   : "roundrobin" | "gauntlet"
      - seeds             : int  (gauntlet)
      - rounds            : int
      - games_per_round   : int  (default 2)
      - sprt              : dict {elo0, elo1, alpha, beta, model}
      - resign            : dict {movecount, score}
      - draw              : dict {movenumber, movecount, score}

    Engines come from ``spec.tournament.engines`` — each entry is a
    dict with at minimum ``name`` and ``cmd`` (engine binary path).
    Optional: ``args``, ``dir``.
    """
    t = spec.tournament.template
    engines = spec.tournament.engines

    cmd: list[str] = [spec.binary_path]

    # Per-engine: -engine cmd=... name=... [args=...] [dir=...]
    for eng in engines:
        if "cmd" not in eng:
            raise ValueError(f"engine missing 'cmd': {eng!r}")
        e: list[str] = ["-engine", f"cmd={eng['cmd']}", f"name={eng.get('name', eng['cmd'])}"]
        if eng.get("args"):
            e.append(f"args={eng['args']}")
        if eng.get("dir"):
            e.append(f"dir={eng['dir']}")
        if "tc" in t:
            e.append(f"tc={t['tc']}")
        cmd.extend(e)

    # -each: tournament-template options applied to all engines.
    each: list[str] = []
    if "hash" in t:
        each.append(f"option.Hash={t['hash']}")
    if "threads" in t:
        each.append(f"option.Threads={t['threads']}")
    if "ponder" in t:
        each.append(f"option.Ponder={'true' if t['ponder'] else 'false'}")
    if "tablebase" in t:
        each.append(f"option.SyzygyPath={t['tablebase']}")
    if each:
        cmd.append("-each")
        cmd.extend(each)

    # Tournament setup
    if "games_in_parallel" in t:
        cmd.extend(["-concurrency", str(t["games_in_parallel"])])
    if "rounds" in t:
        cmd.extend(["-rounds", str(t["rounds"])])
    if "games_per_round" in t:
        cmd.extend(["-games", str(t["games_per_round"])])
    if t.get("tournament_type") == "gauntlet":
        cmd.extend(["-tournament", "gauntlet"])
        if "seeds" in t:
            cmd.extend(["-seeds", str(t["seeds"])])
    elif t.get("tournament_type") == "roundrobin":
        cmd.extend(["-tournament", "roundrobin"])

    # Opening book
    if "book" in t:
        fmt = t.get("book_format", "epd")
        cmd.extend(["-openings", f"file={t['book']}", f"format={fmt}"])

    # SPRT
    if "sprt" in t and t["sprt"]:
        s = t["sprt"]
        cmd.extend([
            "-sprt",
            f"elo0={s['elo0']}",
            f"elo1={s['elo1']}",
            f"alpha={s.get('alpha', 0.05)}",
            f"beta={s.get('beta', 0.05)}",
            f"model={s.get('model', 'normalized')}",
        ])

    # Adjudication
    if "resign" in t and t["resign"]:
        r = t["resign"]
        cmd.extend(["-resign", f"movecount={r['movecount']}", f"score={r['score']}"])
    if "draw" in t and t["draw"]:
        d = t["draw"]
        cmd.extend([
            "-draw",
            f"movenumber={d['movenumber']}",
            f"movecount={d['movecount']}",
            f"score={d['score']}",
        ])

    # Output: PGN append, fastchess output format (we parse PGN ourselves
    # for stats, but fastchess's stdout is still captured to logs).
    cmd.extend([
        "-pgnout",
        f"file={spec.pgn_path}",
        "notation=san",
        "append=true",
    ])
    cmd.extend(["-output", "format=fastchess"])

    return cmd


def _popen_kwargs() -> dict:
    """Cross-platform process-group isolation, copied from
    ``~/Projects/sturddle-2/tools/tuneup/spsa/worker.py``."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class FastchessRunner:
    """Owns one fastchess subprocess at a time.

    Single-instance: calling ``start`` while already running raises.
    The orchestrator enforces single-active across tournaments; this
    class only enforces single-active inside itself.
    """

    def __init__(self, binary_path: str | None = None) -> None:
        self._binary_path = binary_path
        self._proc: asyncio.subprocess.Process | None = None
        self._stop_requested = False
        self._supervisor: asyncio.Task | None = None
        self._drain_tasks: list[asyncio.Task] = []
        self._on_event: EventCallback | None = None
        self._spec: RunSpec | None = None

    @property
    def binary_path(self) -> str | None:
        return self._binary_path

    def set_binary_path(self, path: str | None) -> None:
        """Update the configured fastchess binary path.

        Takes effect on the next ``start()``; a currently-running
        subprocess is unaffected (its argv is frozen at spawn time).
        """
        self._binary_path = path

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @staticmethod
    def detect_binary(configured_path: str | None) -> str | None:
        """Return the fastchess binary to use, or ``None`` if not found.

        Resolution order:
          1. ``configured_path`` if it's an executable file.
          2. ``shutil.which("fastchess")`` on PATH.
          3. ``None``.
        """
        if configured_path:
            from pathlib import Path as _P
            p = _P(configured_path)
            if p.is_file():
                return str(p)
        return shutil.which("fastchess")

    async def start(self, spec: RunSpec, on_event: EventCallback) -> None:
        if self.is_running():
            raise RuntimeError("FastchessRunner already running")

        binary = self.detect_binary(self._binary_path)
        if not binary:
            raise FileNotFoundError(
                f"fastchess binary not found (configured: {self._binary_path!r})"
            )

        self._on_event = on_event
        self._spec = spec
        self._stop_requested = False

        cmd = build_command(spec)
        # Override the binary in the cmd in case detect_binary resolved it
        # (RunSpec.binary_path may differ from what we actually launch).
        cmd[0] = binary

        spec.work_dir.mkdir(parents=True, exist_ok=True)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)

        log.info("starting fastchess: %s", " ".join(str(c) for c in cmd))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(spec.work_dir),
            **_popen_kwargs(),
        )

        # Pipe drains: stream stdout/stderr → log file (open in append mode).
        log_file = spec.log_path.open("a", encoding="utf-8", errors="replace")
        log_file.write(
            f"\n=== fastchess run for tournament {spec.tournament.id} ===\n"
        )
        log_file.flush()
        self._drain_tasks = [
            asyncio.create_task(
                self._drain(self._proc.stdout, log_file, "out"),
                name="fastchess-stdout",
            ),
            asyncio.create_task(
                self._drain(self._proc.stderr, log_file, "err"),
                name="fastchess-stderr",
            ),
        ]

        await self._emit("started", {"pid": self._proc.pid})

        # Supervisor task watches for exit and emits the terminal event.
        # Detached: callers don't await it; ``stop()`` cancels it cleanly.
        self._supervisor = asyncio.create_task(
            self._supervise(log_file), name="fastchess-supervisor"
        )

    async def stop(self) -> None:
        """Hard-kill the subprocess. Idempotent."""
        if self._proc is None:
            return
        if self._proc.returncode is not None:
            return  # already exited
        self._stop_requested = True
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        # supervisor will observe the exit and emit "stopped"
        if self._supervisor is not None:
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass

    # ----- internals --------------------------------------------------------

    async def _drain(
        self,
        stream: asyncio.StreamReader | None,
        log_file: Any,
        tag: str,
    ) -> None:
        """Forward each line of ``stream`` to the log file. Tolerates
        the stream closing or being None."""
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                try:
                    log_file.write(line.decode("utf-8", errors="replace"))
                    log_file.flush()
                except (ValueError, OSError):
                    # log file closed by supervisor — drop the line.
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("drain task (%s) crashed", tag)

    async def _supervise(self, log_file: Any) -> None:
        assert self._proc is not None
        try:
            rc = await self._proc.wait()
        except asyncio.CancelledError:
            return
        # Wait for drain tasks to flush — they exit naturally on EOF.
        for t in self._drain_tasks:
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except asyncio.TimeoutError:
                t.cancel()
            except asyncio.CancelledError:
                pass
        try:
            log_file.close()
        except OSError:
            pass

        if self._stop_requested:
            kind, payload = "stopped", {"rc": rc}
        elif rc == 0:
            kind, payload = "done", {"rc": rc}
        else:
            kind, payload = "runner_crash", {"rc": rc}

        await self._emit(kind, payload)

    async def _emit(self, kind: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(kind, payload)
        except Exception:
            log.exception("on_event callback raised for %s", kind)
