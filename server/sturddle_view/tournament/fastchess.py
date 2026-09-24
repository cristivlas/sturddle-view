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
import shutil
import signal
import subprocess
from collections import deque
from pathlib import Path

from .. import is_windows
from .._runtime import proxy_argv_prefix
from .._win_job import assign_to_job, close_job, create_job, spawn_in_job, wait_for_pid_exit
from ..engines import (
    UCI_OPT_HASH,
    UCI_OPT_OWNBOOK,
    UCI_OPT_PONDER,
    UCI_OPT_SYZYGY_PATH,
    UCI_OPT_THREADS,
)
from ..env_utils import env_choice, env_float
from ..play.opening_lines import is_epd_book
from .pgn_stats import (
    SPRT_ALPHA,
    SPRT_BETA,
    SPRT_DEFAULT_ALPHA,
    SPRT_DEFAULT_BETA,
    SPRT_ELO0,
    SPRT_ELO1,
    SPRT_MODEL,
    SPRT_MODEL_LOGISTIC,
    SPRT_MODEL_NORMALIZED,
)
from .proxy import PROXY_SECRET_ENV
from .runner import (
    EVT_DONE,
    EVT_RUNNER_CRASH,
    EVT_RUNNER_LOG,
    EVT_STARTED,
    EVT_STOPPED,
    RC_KEY,
    STDERR_TAIL_KEY,
    EventCallback,
    RunSpec,
)
from .store import (
    ENGINE_REF_ARGS,
    ENGINE_REF_CMD,
    ENGINE_REF_DIR,
    ENGINE_REF_ENV,
    ENGINE_REF_NAME,
    ENGINE_REF_OPTIONS,
)
from .template import (
    ALLOW_OVERSUBSCRIBE_KEY,
    GAMES_IN_PARALLEL_KEY,
    PIN_AFFINITY_KEY,
    PONDER_KEY,
    SEED_KEY,
    SPRT_KEY,
    TOURNAMENT_TYPE_KEY,
    TYPE_GAUNTLET,
    TYPE_ROUNDROBIN,
)

log = logging.getLogger(__name__)

# Recent stderr/stdout lines retained for runner_crash diagnostics.
_STDERR_TAIL_MAX = 40

# Stand-in exit code when the process is known dead but Windows could not
# report its code. Non-zero so it classifies as a crash, and distinct from
# the small negatives POSIX uses to encode terminating signals.
_UNKNOWN_EXIT_RC = -9999


# Template keys (see tournament.template for the shared ones).
_TPL_TC = "tc"
_TPL_ROUNDS = "rounds"
_TPL_GAMES_PER_ROUND = "games_per_round"
_TPL_SEEDS = "seeds"
_TPL_RESIGN = "resign"
_TPL_DRAW = "draw"
# Adjudication dict keys shared by resign and draw.
_ADJ_MOVECOUNT = "movecount"
_ADJ_SCORE = "score"

# fastchess CLI values.
_ARG_ROUNDS = "-rounds"
_ARG_TOURNAMENT = "-tournament"
_APPEND_ON = "append=true"
# rounds=0 is fastchess's SPRT-unlimited mode (500k rounds).
_SPRT_UNLIMITED_ROUNDS = "0"
# Save cfg.json after every game (fastchess default: 20).
_AUTOSAVE_EVERY_GAME = "1"
_BOOK_FORMAT_EPD = "epd"
_BOOK_FORMAT_PGN = "pgn"
_FC_LOG_LEVELS = frozenset({"trace", "info", "warn", "err", "fatal"})
_FC_LOG_LEVEL_DEFAULT = "info"

# Drain task stream tags; the stderr one picks the crash-diagnostic tail.
_STREAM_OUT = "out"
_STREAM_ERR = "err"
# Windows ERROR_ACCESS_DENIED: assign_to_job on a process already in the Job.
_ERROR_ACCESS_DENIED = 5
# How long the supervisor waits for each drain task to flush after exit.
_DRAIN_FLUSH_TIMEOUT_S = env_float("SV_FASTCHESS_DRAIN_TIMEOUT_S", 2.0, min_value=0.0)


def _sprt_model(model) -> str:
    """fastchess CLI model name. New tournaments default to normalized
    pentanomial; a legacy template may carry "logistic", which is honored so
    fastchess and our LLR recompute use the same model and agree. Anything
    else (incl. the "pentanomial" alias / unset) -> normalized."""
    return SPRT_MODEL_LOGISTIC if model == SPRT_MODEL_LOGISTIC else SPRT_MODEL_NORMALIZED


def _quote_arg(arg: str) -> str:
    """Quote an argument for fastchess's ``args=`` field.

    fastchess parses ``args="A B C"`` by stripping the outer quotes and
    splitting on whitespace. To pass an argument that itself contains
    whitespace (e.g. an engine name like ``"Sturddle 2.5.0"``), wrap it
    in double quotes. Internal quotes get backslash-escaped -- same
    convention as POSIX shells.
    """
    if not arg:
        return '""'
    if " " in arg or "\t" in arg or '"' in arg:
        escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return arg


def _uci_option_value(v) -> str:
    """fastchess option.K=V value text; JSON bools become true/false."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _option_arg(name: str, value) -> str:
    return f"option.{name}={_uci_option_value(value)}"


def _each_options(spec: RunSpec) -> dict[str, object]:
    """UCI options the tournament itself sets on every engine (``-each``).
    UCI knobs come from the global engine defaults snapshotted on the
    RunSpec; the legacy template fields with the same names are ignored.
    When a common opening book feeds the tournament, each engine's own
    book is disabled so it doesn't override/double the shared openings."""
    t = spec.tournament.template
    opts: dict[str, object] = {}
    if spec.engine_default_hash_mb is not None:
        opts[UCI_OPT_HASH] = spec.engine_default_hash_mb
    if spec.engine_default_threads is not None:
        opts[UCI_OPT_THREADS] = spec.engine_default_threads
    if PONDER_KEY in t:
        opts[UCI_OPT_PONDER] = bool(t[PONDER_KEY])
    if spec.engine_default_syzygy_path:
        opts[UCI_OPT_SYZYGY_PATH] = spec.engine_default_syzygy_path
    if spec.engine_default_book_path:
        opts[UCI_OPT_OWNBOOK] = False
    return opts


# Restart each engine process between games (fastchess restart=on).
_RESTART_ON = "restart=on"


def build_command(spec: RunSpec) -> list[str]:
    """Translate a ``RunSpec`` into a fastchess argv.

    Template fields recognized:
      - tc                : str, fastchess tc= format ("10+0.1", "40/60", ...)
      - ponder            : bool
      - games_in_parallel : int  (fastchess -concurrency)
      - tournament_type   : "roundrobin" | "gauntlet"
      - seeds             : int  (gauntlet)
      - rounds            : int
      - games_per_round   : int  (default 2)
      - sprt              : dict {elo0, elo1, alpha, beta, model?}; model
                            defaults to normalized, legacy "logistic" honored
      - resign            : dict {movecount, score, twosided?}
      - draw              : dict {movenumber, movecount, score}

    Legacy template fields (``hash``, ``threads``, ``tablebase``,
    ``book``, ``book_format``) are tolerated on read but ignored --
    Hash/Threads/SyzygyPath/book come from the tournament's frozen
    ``engine_defaults`` snapshot, propagated via ``RunSpec``.

    Engines come from ``spec.tournament.engines`` -- each entry is a
    dict with at minimum ``name`` and ``cmd`` (engine binary path).
    Optional: ``args``, ``dir``, ``options`` (per-engine UCI options
    snapshotted from the registry at create/edit time; options the
    tournament sets itself are skipped, see _each_options).
    """
    t = spec.tournament.template
    engines = spec.tournament.engines
    each_options = _each_options(spec)
    # Per-engine snapshot values for these are skipped so the tournament
    # stays the single source of truth; anything it leaves unset falls
    # back to the engine's own setting.
    managed_options = {name.casefold() for name in each_options}
    tc = t.get(_TPL_TC)

    cmd: list[str] = [spec.binary_path]

    # When proxy broadcast is configured, wrap each engine's
    # cmd= so fastchess spawns the proxy script with the real engine as
    # an argument. The proxy forwards stdio transparently and POSTs a
    # copy to the GUI server.
    proxy_url = spec.proxy_broadcast_url
    proxy_secret = spec.proxy_secret
    proxy_enabled = bool(proxy_url and proxy_secret)

    # Per-engine: -engine cmd=... name=... [args=...] [dir=...]
    for eng in engines:
        if ENGINE_REF_CMD not in eng:
            raise ValueError(f"engine missing '{ENGINE_REF_CMD}': {eng!r}")
        eng_cmd = eng[ENGINE_REF_CMD]
        engine_name = eng.get(ENGINE_REF_NAME, eng_cmd)
        # Per-engine launch profile (registered via the Engine Settings
        # dialog). ``args`` is a list[str] today; older callers / tests may
        # pass a single pre-joined string -- accept both for resilience.
        eng_args_raw = eng.get(ENGINE_REF_ARGS) or []
        if isinstance(eng_args_raw, str):
            eng_args: list[str] = [eng_args_raw] if eng_args_raw else []
        else:
            eng_args = [str(a) for a in eng_args_raw]
        eng_env = eng.get(ENGINE_REF_ENV) or {}
        e: list[str] = ["-engine"]
        if proxy_enabled:
            # cmd = python; args = the proxy invocation + the real
            # engine. The proxy_id is generated per-process by the
            # proxy script itself (not baked into argv). fastchess
            # reuses one engine spec across multiple concurrent
            # game-slots when ``-concurrency > 1``; each spawned slot
            # process must get a distinct proxy_id, which only the
            # proxy itself can mint at startup.
            # Secret is passed via PROXY_SECRET_ENV in the environment
            # (see FastchessRunner.start). Keeping it out of argv hides
            # it from `ps` / `/proc/<pid>/cmdline`.
            # Per-engine env overrides ride along as repeatable
            # ``--env KEY=VAL`` flags; the proxy applies them when it
            # spawns the real engine.
            _proxy_prefix = proxy_argv_prefix()
            # _proxy_prefix[0] is the executable; [1:] are the subcommand
            # args that route to the proxy (differs between dev and frozen).
            parts: list[str] = list(_proxy_prefix[1:]) + [
                "--broadcast-url", proxy_url,
                "--engine-name", _quote_arg(engine_name),
            ]
            for k, v in eng_env.items():
                parts.extend(["--env", _quote_arg(f"{k}={v}")])
            parts.extend(["--", _quote_arg(eng_cmd)])
            for a in eng_args:
                parts.append(_quote_arg(a))
            e.append(f"cmd={_proxy_prefix[0]}")
            e.append(f"args={' '.join(parts)}")
        else:
            # No-proxy path is test-only; per-engine env is silently
            # dropped here because fastchess has no per-engine env knob.
            # Production always runs with the proxy enabled.
            if eng_env:
                log.warning(
                    "engine %s: per-engine env ignored (proxy disabled)",
                    engine_name,
                )
            e.append(f"cmd={eng_cmd}")
            if eng_args:
                e.append(f"args={' '.join(_quote_arg(a) for a in eng_args)}")
        e.append(f"name={engine_name}")
        eng_dir = eng.get(ENGINE_REF_DIR)
        if eng_dir:
            e.append(f"dir={eng_dir}")
        for k, v in (eng.get(ENGINE_REF_OPTIONS) or {}).items():
            if k.casefold() in managed_options:
                continue
            e.append(_option_arg(k, v))
        if tc is not None:
            e.append(f"tc={tc}")
        cmd.extend(e)

    # -each: options applied to all engines.
    each = [_option_arg(name, value) for name, value in each_options.items()]
    if t.get("restart_engines"):
        each.append(_RESTART_ON)
    if each:
        cmd.append("-each")
        cmd.extend(each)

    # Tournament setup
    games_in_parallel = t.get(GAMES_IN_PARALLEL_KEY)
    if games_in_parallel is not None:
        cmd.extend(["-concurrency", str(games_in_parallel)])
    # Without this, fastchess refuses concurrency > logical CPUs. Our
    # own rescheck has already either passed or warned the user; the
    # flag tells fastchess to honor the same intent.
    if t.get(ALLOW_OVERSUBSCRIBE_KEY):
        cmd.append("-force-concurrency")
    if t.get(PIN_AFFINITY_KEY):
        cmd.append("-use-affinity")
    sprt = t.get(SPRT_KEY)
    rounds = t.get(_TPL_ROUNDS)
    if sprt:
        cmd.extend([_ARG_ROUNDS, _SPRT_UNLIMITED_ROUNDS])
    elif rounds is not None:
        cmd.extend([_ARG_ROUNDS, str(rounds)])
    games_per_round = t.get(_TPL_GAMES_PER_ROUND)
    if games_per_round is not None:
        cmd.extend(["-games", str(games_per_round)])
    tournament_type = t.get(TOURNAMENT_TYPE_KEY)
    if tournament_type in (TYPE_GAUNTLET, TYPE_ROUNDROBIN):
        cmd.extend([_ARG_TOURNAMENT, tournament_type])
    seeds = t.get(_TPL_SEEDS)
    if tournament_type == TYPE_GAUNTLET and seeds is not None:
        cmd.extend(["-seeds", str(seeds)])

    if spec.tournament.name:
        cmd.extend(["-event", spec.tournament.name])

    # Pinned seed for fastchess's PRNG (opening shuffle, etc) so the
    # opening sequence is reproducible for this tournament's lifetime.
    seed = t.get(SEED_KEY)
    if seed is not None:
        cmd.extend(["-srand", str(seed)])

    # Save cfg.json after every game so a server crash loses at most
    # one in-flight game's worth of recorded progress.
    cmd.extend(["-autosaveinterval", _AUTOSAVE_EVERY_GAME])

    # Opening book -- global default from settings; legacy template
    # ``book``/``book_format`` fields are ignored. Format inferred from
    # the file extension (.epd -> epd, anything else -> pgn) since the
    # settings tab exposes only the path + plies.
    if spec.engine_default_book_path:
        path = spec.engine_default_book_path
        fmt = _BOOK_FORMAT_EPD if is_epd_book(path) else _BOOK_FORMAT_PGN
        opening = ["-openings", f"file={path}", f"format={fmt}"]
        if spec.engine_default_book_plies is not None:
            opening.append(f"plies={spec.engine_default_book_plies}")
        if spec.engine_default_book_order:
            opening.append(f"order={spec.engine_default_book_order}")
        cmd.extend(opening)

    # SPRT -- honor the template's model (normalized default) so fastchess and
    # our LLR recompute agree. New tournaments are always normalized.
    if sprt:
        cmd.extend([
            "-sprt",
            f"{SPRT_ELO0}={sprt[SPRT_ELO0]}",
            f"{SPRT_ELO1}={sprt[SPRT_ELO1]}",
            f"{SPRT_ALPHA}={sprt.get(SPRT_ALPHA, SPRT_DEFAULT_ALPHA)}",
            f"{SPRT_BETA}={sprt.get(SPRT_BETA, SPRT_DEFAULT_BETA)}",
            f"{SPRT_MODEL}={_sprt_model(sprt.get(SPRT_MODEL))}",
        ])

    # Adjudication
    resign = t.get(_TPL_RESIGN)
    if resign:
        cmd.extend([
            "-resign",
            f"{_ADJ_MOVECOUNT}={resign[_ADJ_MOVECOUNT]}",
            f"{_ADJ_SCORE}={resign[_ADJ_SCORE]}",
        ])
        if resign.get("twosided"):
            cmd.append("twosided=true")
    draw = t.get(_TPL_DRAW)
    if draw:
        cmd.extend([
            "-draw",
            f"movenumber={draw['movenumber']}",
            f"{_ADJ_MOVECOUNT}={draw[_ADJ_MOVECOUNT]}",
            f"{_ADJ_SCORE}={draw[_ADJ_SCORE]}",
        ])
    # Tablebase (Syzygy) adjudication: only -tb <path>, leaving fastchess
    # at its defaults for piece count / 50-move / result type. Gated on a
    # configured SyzygyPath (the same one passed as the UCI option above).
    if t.get("tb_adjudication") and spec.engine_default_syzygy_path:
        cmd.extend(["-tb", spec.engine_default_syzygy_path])

    # Output: PGN append, fastchess output format (we parse PGN ourselves
    # for stats, but fastchess's stdout is still captured to logs).
    cmd.extend([
        "-pgnout",
        f"file={spec.pgn_path}",
        "notation=san",
        _APPEND_ON,
    ])
    cmd.extend(["-output", "format=fastchess"])

    # fastchess writes directly to log_path; Python drain handles events only.
    log_level = env_choice("SV_FASTCHESS_LOG_LEVEL", _FC_LOG_LEVEL_DEFAULT, _FC_LOG_LEVELS)
    cmd.extend(["-log", f"file={spec.log_path}", f"level={log_level}", _APPEND_ON])

    # Always pass outname= so fastchess writes its scoreboard snapshot
    # (used post-run for the games-played reconcile check). Only pass
    # file= when the snapshot already exists -- crash-recovery path
    # only -- since fastchess errors out if file= points at a missing
    # path (cli.cpp:439 throws fastchess_exception).
    cfg = ["-config", f"outname={spec.config_path}"]
    if spec.config_path.exists():
        cfg.insert(1, f"file={spec.config_path}")
    cmd.extend(cfg)

    return cmd


def _popen_kwargs() -> dict:
    """Cross-platform process-group isolation kwargs (POSIX vs Windows)."""
    if is_windows():
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


class FastchessRunner:
    """Owns one fastchess subprocess; ``start`` while running raises."""

    def __init__(self, binary_path: str | None = None) -> None:
        self._binary_path = binary_path
        self._proc: asyncio.subprocess.Process | None = None
        # Exit code observed via the OS process handle when asyncio could
        # not see it (surviving slot holding the pipes). None while running.
        self._exited_rc: int | None = None
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
        if self._proc is None or self._exited_rc is not None:
            return False
        return self._proc.returncode is None

    @staticmethod
    def detect_binary(configured_path: str | None) -> str | None:
        """Return the fastchess binary to use, or ``None`` if not found.

        Resolution order:
          1. ``configured_path`` if it's an executable file.
          2. ``shutil.which("fastchess")`` on PATH.
          3. ``None``.
        """
        if configured_path:
            p = Path(configured_path)
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
        self._exited_rc = None
        self._stop_requested = False
        self._stderr_tail.clear()
        self._stdout_tail.clear()

        cmd = build_command(spec)
        # Override the binary in the cmd in case detect_binary resolved it
        # (RunSpec.binary_path may differ from what we actually launch).
        cmd[0] = binary

        spec.work_dir.mkdir(parents=True, exist_ok=True)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)

        # Should always be cfg_exists=false post-wipe; any true here
        # flags a wipe bypass and is worth investigating.
        seed = spec.tournament.template.get(SEED_KEY)
        log.info(
            "starting fastchess: tournament=%s cfg_exists=%s seed=%s pgn=%s",
            spec.tournament.id,
            spec.config_path.exists(),
            seed if seed is not None else "<unset>",
            spec.pgn_path,
        )
        log.info("fastchess argv: %s", " ".join(str(c) for c in cmd))

        # Pass the proxy secret via env (inherited by fastchess and the
        # engine slots it spawns) rather than argv, so it isn't visible
        # via `ps` / `/proc/<pid>/cmdline` to other local users.
        env = os.environ.copy()
        if spec.proxy_secret:
            env[PROXY_SECRET_ENV] = spec.proxy_secret

        # Create the per-tournament Job BEFORE spawn so the process can
        # be created already inside it (atomic via spawn_in_job).
        if is_windows():
            try:
                self._job_handle = create_job()
            except OSError:
                log.error("Job Object creation failed", exc_info=True)

        with spawn_in_job(self._job_handle):
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(spec.work_dir),
                env=env,
                **_popen_kwargs(),
            )

        # Post-spawn setup: any failure here must kill the proc, otherwise
        # we leak a running fastchess (no supervisor => no auto-cleanup).
        try:
            # Belt-and-suspenders: if the atomic path silently no-op'd
            # (e.g. interception bypass), make sure fastchess is in the Job.
            if self._job_handle is not None:
                try:
                    assign_to_job(self._job_handle, self._proc.pid)
                except OSError as e:
                    # Already in this Job: expected in the atomic path.
                    # Anything else is real.
                    if getattr(e, "winerror", None) != _ERROR_ACCESS_DENIED:
                        log.error("assign_to_job failed for pid=%d", self._proc.pid, exc_info=True)

            # Pipe drains: emit runner_log events; fastchess writes the log file.
            self._drain_tasks = [
                asyncio.create_task(
                    self._drain(self._proc.stdout, _STREAM_OUT),
                    name="fastchess-stdout",
                ),
                asyncio.create_task(
                    self._drain(self._proc.stderr, _STREAM_ERR),
                    name="fastchess-stderr",
                ),
            ]

            await self._emit(EVT_STARTED, {"pid": self._proc.pid})

            # Supervisor task watches for exit and emits the terminal event.
            # Detached: callers don't await it; ``stop()`` cancels it cleanly.
            self._supervisor = asyncio.create_task(
                self._supervise(), name="fastchess-supervisor"
            )
        except BaseException:
            await self._abort_setup()
            raise

    # How long to wait between SIGTERM and SIGKILL on Stop. Long enough
    # for fastchess to flush a final saveJson() (post-game bookkeeping is
    # microseconds, but the in-flight game's engines may need to drain
    # UCI traffic). Short enough that the UI doesn't hang on a runaway
    # subprocess.
    _STOP_GRACE_SECONDS = env_float("SV_FASTCHESS_STOP_GRACE_S", 2.0, min_value=0.0)

    async def stop(self) -> None:
        """Stop the subprocess. Idempotent.

        Windows: closes the per-tournament Job -> KILL_ON_JOB_CLOSE
        kills the entire tree synchronously. POSIX: SIGTERM + grace
        for fastchess to flush resume state, escalates to SIGKILL.
        """
        if self._proc is None or self._supervisor is None:
            log.info("stop: no process to stop")
            return
        if not self.is_running():
            # Covers both a normally-reaped exit and one seen only via the
            # OS handle -- in the latter the terminal event has already
            # been emitted, so setting _stop_requested now would relabel a
            # crash as a user-requested stop.
            rc = self._proc.returncode if self._proc.returncode is not None else self._exited_rc
            log.info("stop: process already exited rc=%s", rc)
            return
        self._stop_requested = True
        pid = self._proc.pid

        if is_windows():
            # Closing the Job triggers KILL_ON_JOB_CLOSE -- fastchess +
            # every descendant dies synchronously in the OS. No orphans,
            # no inherited pipe handles to wedge proc.wait().
            log.info("stop: closing Job for pid=%d (kills tree)", pid)
            close_job(self._job_handle)
            self._job_handle = None
        else:
            # SIGTERM the whole process group (set via start_new_session in
            # _popen_kwargs) so engines + proxies get the chance to clean
            # up too -- proc.terminate() would only signal fastchess.
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
            # TODO(#7): the grace timeout fires when the supervisor's
            # terminal chain outlasts the grace -- not necessarily because fastchess
            # ignored SIGTERM. Only escalate to SIGKILL if proc.returncode
            # is None. Needs Linux/macOS to test.
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

        # Wait for the supervisor to finish its terminal-event chain. The
        # chain is bounded (state update + non-blocking publishes + bounded
        # tailer drain), so an unbounded await is safe and required: we
        # must not return while cleanup may still race with the next start.
        try:
            await asyncio.shield(self._supervisor)
            log.info("stop: supervisor returned for pid=%d", pid)
        except asyncio.CancelledError:
            pass

    async def _abort_setup(self) -> None:
        """Tear down a half-spawned runner when start() fails post-spawn.
        Without this, a running fastchess leaks (no supervisor)."""
        proc = self._proc
        for t in self._drain_tasks:
            t.cancel()
        self._drain_tasks = []
        if is_windows() and self._job_handle is not None:
            close_job(self._job_handle)  # KILL_ON_JOB_CLOSE => synchronous tree kill
            self._job_handle = None
        elif proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        if proc is not None:
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                log.error("abort_setup: proc.wait raised", exc_info=True)
        self._proc = None

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
                    tail = self._stderr_tail if tag == _STREAM_ERR else self._stdout_tail
                    tail.append(stripped)
                    await self._emit(EVT_RUNNER_LOG, {"stream": tag, "line": stripped})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("drain task (%s) crashed", tag, exc_info=True)

    async def _wait_for_exit(self, pid: int) -> int | None:
        """Return fastchess's exit code once the process is gone.

        POSIX: plain ``proc.wait()``. Windows: also wait on the OS
        process handle and take whichever resolves first. ``proc.wait()``
        alone additionally requires every inherited stdio pipe to hit
        EOF, so an engine slot that outlives an externally-killed
        fastchess would otherwise wedge the supervisor forever.
        """
        assert self._proc is not None
        proc_wait = asyncio.ensure_future(self._proc.wait())
        if not is_windows():
            return await proc_wait
        handle_wait = asyncio.ensure_future(wait_for_pid_exit(pid))
        try:
            done, _ = await asyncio.wait(
                (proc_wait, handle_wait), return_when=asyncio.FIRST_COMPLETED,
            )
            # proc.wait() carries asyncio's own bookkeeping, so prefer it
            # when both resolved; otherwise use the OS exit code.
            if proc_wait in done:
                return proc_wait.result()
            # asyncio never saw this exit (a surviving slot still holds
            # its pipes), so ``_proc.returncode`` stays None -- record the
            # exit ourselves so ``is_running()`` stops reporting True.
            rc = handle_wait.result()
            self._exited_rc = _UNKNOWN_EXIT_RC if rc is None else rc
            return self._exited_rc
        finally:
            # On the usual path proc.wait() wins and this cancels the
            # handle wait, which unblocks its worker immediately.
            for fut in (proc_wait, handle_wait):
                if not fut.done():
                    fut.cancel()

    async def _supervise(self) -> None:
        assert self._proc is not None
        pid = self._proc.pid
        try:
            rc = await self._wait_for_exit(pid)
            log.info("supervise: pid=%d exited rc=%s", pid, rc)
        except asyncio.CancelledError:
            return
        # Reap any surviving slots before draining -- an external kill
        # (taskkill without /T) leaves them holding the inherited stdio
        # pipes, which the drains below would otherwise wait on forever.
        # Cancellation skips this; stop() owns the Job in that path.
        close_job(self._job_handle)
        self._job_handle = None
        # Wait for drain tasks to flush -- they exit naturally on EOF.
        for t in self._drain_tasks:
            try:
                await asyncio.wait_for(t, timeout=_DRAIN_FLUSH_TIMEOUT_S)
            except asyncio.TimeoutError:
                t.cancel()
            except asyncio.CancelledError:
                pass
        # Python 3.12+ exposes Process.close(); use it when available so the
        # underlying transport is released immediately rather than waiting for GC.
        if hasattr(self._proc, "close"):
            self._proc.close()

        payload: dict = {RC_KEY: rc}
        if self._stop_requested:
            kind = EVT_STOPPED
        elif rc == 0:
            kind = EVT_DONE
        else:
            # Prefer stderr; fall back to stdout (fastchess emits some
            # CLI errors there) so the UI never gets an empty diagnostic.
            tail = list(self._stderr_tail) or list(self._stdout_tail)
            kind = EVT_RUNNER_CRASH
            payload[STDERR_TAIL_KEY] = tail
            log.error(
                "runner_crash: rc=%s\n%s", rc, "\n".join(tail) if tail else "(no output captured)",
            )

        await self._emit(kind, payload)

    async def _emit(self, kind: str, payload: dict) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(kind, payload)
        except Exception:
            log.error("on_event callback raised for %s", kind, exc_info=True)
