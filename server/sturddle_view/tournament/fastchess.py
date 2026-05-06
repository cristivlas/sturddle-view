"""FastchessRunner: Runner protocol over the fastchess CLI.

- Cross-platform process-group isolation (CREATE_NEW_PROCESS_GROUP / start_new_session)
  + Windows Job Object so kills are targeted and cascade to descendants.
- Stdout/stderr drained to ``logs/fastchess.log`` via background tasks
  (a full pipe never deadlocks the wrapper).
- ``stop()`` is an idempotent hard kill (``proc.kill()`` + ``proc.wait()``).
- Clean exit vs killed is distinguished and surfaced as ``done`` / ``stopped``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from collections import deque

from .._win_job import assign_to_job, close_job, create_job, spawn_in_job
from .runner import EventCallback, RunSpec


# Recent stderr/stdout lines retained for runner_crash diagnostics.
_STDERR_TAIL_MAX = 40

# Lines matching this pattern are kept in the crash tail but not
# forwarded as runner_log events (they would flood the UI uselessly).
# TODO: consider a "System" settings category with user-editable log filters
# (hot-reload and perf implications TBD before exposing in UI).
_LOG_FILTER = re.compile(
    r"^Warning; Last info string with score not found from"
)


def _quote_arg(arg: str) -> str:
    """Quote an argument for fastchess's ``args=`` field.

    fastchess parses ``args="A B C"`` by stripping the outer quotes and
    splitting on whitespace. To pass an argument that itself contains
    whitespace (e.g. an engine name like ``"Sturddle 2.5.0"``), wrap it
    in double quotes. Internal quotes get backslash-escaped — same
    convention as POSIX shells.
    """
    if not arg:
        return '""'
    if " " in arg or "\t" in arg or '"' in arg:
        escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return arg


log = logging.getLogger(__name__)


def build_command(spec: RunSpec) -> list[str]:
    """Translate a ``RunSpec`` into a fastchess argv.

    Template fields recognized (see ``docs/tournament-spec.md``):
      - tc                : str, fastchess tc= format ("10+0.1", "40/60", ...)
      - ponder            : bool
      - games_in_parallel : int  (fastchess -concurrency)
      - tournament_type   : "roundrobin" | "gauntlet"
      - seeds             : int  (gauntlet)
      - rounds            : int
      - games_per_round   : int  (default 2)
      - sprt              : dict {elo0, elo1, alpha, beta, model}
      - resign            : dict {movecount, score}
      - draw              : dict {movenumber, movecount, score}

    Legacy template fields (``hash``, ``threads``, ``tablebase``,
    ``book``, ``book_format``) are tolerated on read but ignored —
    Hash/Threads/SyzygyPath/book come from the tournament's frozen
    ``engine_defaults`` snapshot, propagated via ``RunSpec``.

    Engines come from ``spec.tournament.engines`` — each entry is a
    dict with at minimum ``name`` and ``cmd`` (engine binary path).
    Optional: ``args``, ``dir``.
    """
    t = spec.tournament.template
    engines = spec.tournament.engines

    cmd: list[str] = [spec.binary_path]

    # Slice 9b: when proxy broadcast is configured, wrap each engine's
    # cmd= so fastchess spawns the proxy script with the real engine as
    # an argument. The proxy forwards stdio transparently and POSTs a
    # copy to the GUI server.
    proxy_url = spec.proxy_broadcast_url
    proxy_secret = spec.proxy_secret
    proxy_enabled = bool(proxy_url and proxy_secret)

    # Per-engine: -engine cmd=... name=... [args=...] [dir=...]
    for eng in engines:
        if "cmd" not in eng:
            raise ValueError(f"engine missing 'cmd': {eng!r}")
        engine_name = eng.get("name", eng["cmd"])
        e: list[str] = ["-engine"]
        if proxy_enabled:
            # cmd = python; args = the proxy invocation + the real
            # engine. The proxy_id is generated per-process by the
            # proxy script itself (not baked into argv). fastchess
            # reuses one engine spec across multiple concurrent
            # game-slots when ``-concurrency > 1``; each spawned slot
            # process must get a distinct proxy_id, which only the
            # proxy itself can mint at startup.
            # Secret is passed via SV_PROXY_SECRET in the environment
            # (see FastchessRunner._spawn). Keeping it out of argv hides
            # it from `ps` / `/proc/<pid>/cmdline`.
            proxy_args = " ".join([
                "-m", "sturddle_view.tournament.proxy",
                "--broadcast-url", proxy_url,
                "--engine-name", _quote_arg(engine_name),
                "--",
                _quote_arg(eng["cmd"]),
            ])
            if eng.get("args"):
                proxy_args = f"{proxy_args} {eng['args']}"
            e.append(f"cmd={sys.executable}")
            e.append(f"args={proxy_args}")
        else:
            e.append(f"cmd={eng['cmd']}")
            if eng.get("args"):
                e.append(f"args={eng['args']}")
        e.append(f"name={engine_name}")
        if eng.get("dir"):
            e.append(f"dir={eng['dir']}")
        if "tc" in t:
            e.append(f"tc={t['tc']}")
        cmd.extend(e)

    # -each: options applied to all engines. UCI knobs (Hash/Threads/
    # SyzygyPath) come from the global engine defaults snapshotted on
    # the RunSpec — the legacy template fields with the same names are
    # ignored on purpose (kept readable for old saved templates but no
    # longer authoritative).
    each: list[str] = []
    if spec.engine_default_hash_mb is not None:
        each.append(f"option.Hash={spec.engine_default_hash_mb}")
    if spec.engine_default_threads is not None:
        each.append(f"option.Threads={spec.engine_default_threads}")
    if "ponder" in t:
        each.append(f"option.Ponder={'true' if t['ponder'] else 'false'}")
    if spec.engine_default_syzygy_path:
        each.append(f"option.SyzygyPath={spec.engine_default_syzygy_path}")
    if each:
        cmd.append("-each")
        cmd.extend(each)

    # Tournament setup
    if "games_in_parallel" in t:
        cmd.extend(["-concurrency", str(t["games_in_parallel"])])
    # Without this, fastchess refuses concurrency > logical CPUs. Our
    # own rescheck has already either passed or warned the user; the
    # flag tells fastchess to honor the same intent.
    if t.get("allow_oversubscribe"):
        cmd.append("-force-concurrency")
    if t.get("pin_affinity"):
        cmd.append("-use-affinity")
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

    if spec.tournament.name:
        cmd.extend(["-event", spec.tournament.name])

    # Pinned seed for fastchess's PRNG (opening shuffle, etc). Stable
    # across Stop/Resume cycles — see Resume after Stop in
    # docs/tournament-spec.md.
    if "seed" in t:
        cmd.extend(["-srand", str(t["seed"])])

    # Save cfg.json after every game so a Stop loses at most one
    # in-flight game's worth of resume progress (default is 20).
    cmd.extend(["-autosaveinterval", "1"])

    # Opening book — global default from settings; legacy template
    # ``book``/``book_format`` fields are ignored. Format inferred from
    # the file extension (.epd → epd, anything else → pgn) since the
    # settings tab exposes only the path + plies.
    if spec.engine_default_book_path:
        path = spec.engine_default_book_path
        fmt = "epd" if path.lower().endswith(".epd") else "pgn"
        opening = ["-openings", f"file={path}", f"format={fmt}"]
        if spec.engine_default_book_plies is not None:
            opening.append(f"plies={spec.engine_default_book_plies}")
        if spec.engine_default_book_order:
            opening.append(f"order={spec.engine_default_book_order}")
        cmd.extend(opening)

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

    # FASTCHESS_LOG_LEVEL (trace|info|warn|err|fatal); default info.
    # fastchess writes directly to log_path; Python drain handles events only.
    _fc_log_level = os.environ.get("FASTCHESS_LOG_LEVEL", "info").strip().lower()
    if _fc_log_level in {"trace", "info", "warn", "err", "fatal"}:
        cmd.extend(["-log", f"file={spec.log_path}", f"level={_fc_log_level}", "append=true"])

    # -config drives Resume after Stop. Always pass outname= so fastchess
    # writes its scoreboard snapshot. Only pass file= when the snapshot
    # already exists, since fastchess errors out if file= points at a
    # missing path (cli.cpp:439 throws fastchess_exception).
    cfg = ["-config", f"outname={spec.config_path}"]
    if spec.config_path.exists():
        cfg.insert(1, f"file={spec.config_path}")
    cmd.extend(cfg)

    return cmd


def _popen_kwargs() -> dict:
    """Cross-platform process-group isolation kwargs (POSIX vs Windows)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class FastchessRunner:
    """Owns one fastchess subprocess; ``start`` while running raises."""

    def __init__(self, binary_path: str | None = None) -> None:
        self._binary_path = binary_path
        self._proc: asyncio.subprocess.Process | None = None
        self._stop_requested = False
        self._supervisor: asyncio.Task | None = None
        self._drain_tasks: list[asyncio.Task] = []
        self._on_event: EventCallback | None = None
        self._spec: RunSpec | None = None
        # Tails attached to runner_crash for diagnostics. Both streams
        # captured because fastchess emits early CLI errors to stdout.
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_MAX)
        self._stdout_tail: deque[str] = deque(maxlen=_STDERR_TAIL_MAX)
        # Per-tournament Windows Job: KILL_ON_JOB_CLOSE means closing
        # this handle synchronously kills fastchess + all descendants.
        self._job_handle: int | None = None

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
        self._stderr_tail.clear()
        self._stdout_tail.clear()

        cmd = build_command(spec)
        # Override the binary in the cmd in case detect_binary resolved it
        # (RunSpec.binary_path may differ from what we actually launch).
        cmd[0] = binary

        spec.work_dir.mkdir(parents=True, exist_ok=True)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)

        # Resume-relevant facts up front so logs make a Stop/Start cycle
        # legible without grepping the full argv.
        seed = spec.tournament.template.get("seed")
        is_resume = spec.config_path.exists()
        log.info(
            "starting fastchess: tournament=%s resume=%s cfg_exists=%s seed=%s pgn=%s",
            spec.tournament.id,
            is_resume,
            is_resume,
            seed if seed is not None else "<unset>",
            spec.pgn_path,
        )
        log.info("fastchess argv: %s", " ".join(str(c) for c in cmd))

        # Pass the proxy secret via env (inherited by fastchess and the
        # engine slots it spawns) rather than argv, so it isn't visible
        # via `ps` / `/proc/<pid>/cmdline` to other local users.
        env = os.environ.copy()
        if spec.proxy_secret:
            env["SV_PROXY_SECRET"] = spec.proxy_secret

        # Create the per-tournament Job BEFORE spawn so the process can
        # be created already inside it (atomic via spawn_in_job).
        if sys.platform == "win32":
            try:
                self._job_handle = create_job()
            except OSError:
                log.exception("Job Object creation failed")

        with spawn_in_job(self._job_handle):
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(spec.work_dir),
                env=env,
                **_popen_kwargs(),
            )

        # Belt-and-suspenders: if the atomic path silently no-op'd
        # (e.g. interception bypass), make sure fastchess is in the Job.
        if self._job_handle is not None:
            try:
                assign_to_job(self._job_handle, self._proc.pid)
            except OSError as e:
                # ERROR_ACCESS_DENIED (5) = already in this Job. Expected
                # in the atomic path. Anything else is real.
                if getattr(e, "winerror", None) != 5:
                    log.exception("assign_to_job failed for pid=%d", self._proc.pid)

        # Pipe drains: emit runner_log events; fastchess writes the log file.
        self._drain_tasks = [
            asyncio.create_task(
                self._drain(self._proc.stdout, "out"),
                name="fastchess-stdout",
            ),
            asyncio.create_task(
                self._drain(self._proc.stderr, "err"),
                name="fastchess-stderr",
            ),
        ]

        await self._emit("started", {"pid": self._proc.pid})

        # Supervisor task watches for exit and emits the terminal event.
        # Detached: callers don't await it; ``stop()`` cancels it cleanly.
        self._supervisor = asyncio.create_task(
            self._supervise(), name="fastchess-supervisor"
        )

    # How long to wait between SIGTERM and SIGKILL on Stop. Long enough
    # for fastchess to flush a final saveJson() (post-game bookkeeping is
    # microseconds, but the in-flight game's engines may need to drain
    # UCI traffic). Short enough that the UI doesn't hang on a runaway
    # subprocess.
    _STOP_GRACE_SECONDS = 2.0

    async def stop(self) -> None:
        """Stop the subprocess. Idempotent.

        Windows: closes the per-tournament Job → KILL_ON_JOB_CLOSE
        kills the entire tree synchronously. POSIX: SIGTERM + grace
        for fastchess to flush resume state, escalates to SIGKILL.
        """
        if self._proc is None or self._supervisor is None:
            log.info("stop: no process to stop")
            return
        if self._proc.returncode is not None:
            log.info("stop: process already exited rc=%d", self._proc.returncode)
            return
        self._stop_requested = True
        pid = self._proc.pid

        if sys.platform == "win32":
            # Closing the Job triggers KILL_ON_JOB_CLOSE — fastchess +
            # every descendant dies synchronously in the OS. No orphans,
            # no inherited pipe handles to wedge proc.wait().
            log.info("stop: closing Job for pid=%d (kills tree)", pid)
            close_job(self._job_handle)
            self._job_handle = None
        else:
            # SIGTERM the whole process group (set via start_new_session in
            # _popen_kwargs) so engines + proxies get the chance to clean
            # up too — proc.terminate() would only signal fastchess.
            try:
                pgid = os.getpgid(pid)
            except ProcessLookupError:
                log.info("stop: pid=%d already gone before SIGTERM", pid)
                pgid = None
            if pgid is not None:
                log.info("stop: SIGTERM pgid=%d (leader=%d)", pgid, pid)
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    log.info("stop: pgid=%d already gone before SIGTERM", pgid)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._supervisor),
                    timeout=self._STOP_GRACE_SECONDS,
                )
                log.info("stop: pgid=%s exited gracefully", pgid)
                return
            except asyncio.TimeoutError:
                log.warning("stop: pgid=%d ignored SIGTERM; SIGKILL pgrp", pgid)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        try:
            await asyncio.wait_for(asyncio.shield(self._supervisor), timeout=10.0)
            log.info("stop: supervisor returned for pid=%d", pid)
        except asyncio.TimeoutError:
            log.error("stop: supervisor did not finish 10s after kill (pid=%d)", pid)
        except asyncio.CancelledError:
            pass

    # ----- internals --------------------------------------------------------

    async def _drain(self, stream: asyncio.StreamReader | None, tag: str) -> None:
        """Emit runner_log events from stdout/stderr; fastchess owns the log file."""
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                stripped = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if stripped:
                    tail = self._stderr_tail if tag == "err" else self._stdout_tail
                    tail.append(stripped)
                    if not _LOG_FILTER.match(stripped):
                        await self._emit("runner_log", {"stream": tag, "line": stripped})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("drain task (%s) crashed", tag)

    async def _supervise(self) -> None:
        assert self._proc is not None
        pid = self._proc.pid
        try:
            rc = await self._proc.wait()
            log.info("supervise: pid=%d exited rc=%s", pid, rc)
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
        # Python 3.12+ exposes Process.close(); use it when available so the
        # underlying transport is released immediately rather than waiting for GC.
        if hasattr(self._proc, "close"):
            self._proc.close()

        # Close the Job on natural termination too (done/crash) — kills
        # any descendants fastchess didn't reap. Idempotent for stop().
        close_job(self._job_handle)
        self._job_handle = None

        if self._stop_requested:
            kind, payload = "stopped", {"rc": rc}
        elif rc == 0:
            kind, payload = "done", {"rc": rc}
        else:
            # Prefer stderr; fall back to stdout (fastchess emits some
            # CLI errors there) so the UI never gets an empty diagnostic.
            tail = list(self._stderr_tail) or list(self._stdout_tail)
            kind, payload = "runner_crash", {"rc": rc, "stderr_tail": tail}

        await self._emit(kind, payload)

    async def _emit(self, kind: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(kind, payload)
        except Exception:
            log.exception("on_event callback raised for %s", kind)
