"""UCI engine process lifecycle: spawn, configure, cancel, quit, swap.

Owns the engine subprocess and per-engine launch state. Does NOT own
search loops (HumanVsEngine.{_think_and_play,_run_analysis}) -- the
search-loop callers retain the chess.engine.AnalysisResult and pass it
back into cancel() when stopping a search.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

import chess.engine

from ..engines import _popen_kwargs
from ..events import EVT_UCI_LOG, Event, EventBus

log = logging.getLogger(__name__)

_CANCEL_GRACE_SECONDS = 0.5
_CLOSE_GRACE_SECONDS = 2.0


def _close_and_wait(transport) -> asyncio.Future:
    """Close ``transport`` and return a future resolved when its protocol's
    ``connection_lost`` callback fires.

    The proactor schedules close work; ``connection_lost`` is the real
    done-signal. Already-closing transports and transports without a
    protocol resolve immediately."""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    protocol = transport.get_protocol() if hasattr(transport, "get_protocol") else None
    if protocol is None or transport.is_closing():
        try:
            transport.close()
        except Exception:
            pass
        if not fut.done():
            fut.set_result(None)
        return fut
    orig_conn_lost = protocol.connection_lost

    def _chained(exc):
        try:
            orig_conn_lost(exc)
        finally:
            if not fut.done():
                fut.set_result(None)

    protocol.connection_lost = _chained
    try:
        transport.close()
    except Exception:
        if not fut.done():
            fut.set_result(None)
    return fut


class EngineSupervisor:
    """Single-engine session manager. One instance per HumanVsEngine."""

    def __init__(
        self,
        engine_path: str,
        bus: EventBus,
        settings: Any | None = None,
    ) -> None:
        self._engine_path = engine_path
        self._bus = bus
        self._settings = settings
        self._engine: chess.engine.UciProtocol | None = None
        self._transport: asyncio.SubprocessTransport | None = None
        self._engine_name: str | None = None
        self._options: dict = {}
        self._args: list[str] = []
        self._env: dict[str, str] = {}
        self._uci_log_tasks: set[asyncio.Task] = set()
        # Injection point for tests; production path is chess.engine.popen_uci.
        self._popen_uci = chess.engine.popen_uci

    # ------------------------------------------------------------------
    # configuration accessors (mutated by API layer between spawns)
    # ------------------------------------------------------------------

    @property
    def engine_path(self) -> str:
        return self._engine_path

    @engine_path.setter
    def engine_path(self, value: str) -> None:
        self._engine_path = value

    @property
    def engine_name(self) -> str | None:
        return self._engine_name

    @engine_name.setter
    def engine_name(self, value: str | None) -> None:
        self._engine_name = value

    @property
    def options(self) -> dict:
        return self._options

    @options.setter
    def options(self, value: dict) -> None:
        self._options = dict(value or {})

    @property
    def args(self) -> list[str]:
        return self._args

    @args.setter
    def args(self, value: list[str]) -> None:
        self._args = list(value or [])

    @property
    def env(self) -> dict[str, str]:
        return self._env

    @env.setter
    def env(self, value: dict[str, str]) -> None:
        self._env = dict(value or {})

    @property
    def engine(self) -> chess.engine.UciProtocol | None:
        return self._engine

    @engine.setter
    def engine(self, value: chess.engine.UciProtocol | None) -> None:
        # Setter exists so tests can install stub engines without going through spawn.
        self._engine = value

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def _open_uci(self):
        command: str | list[str] = (
            [self._engine_path, *self._args] if self._args else self._engine_path
        )
        try:
            return await self._popen_uci(command, **_popen_kwargs(self._env))
        except FileNotFoundError as e:
            raise FileNotFoundError(self._engine_path) from e

    async def spawn(
        self,
        overrides: dict | None = None,
        global_defaults: dict | None = None,
    ) -> chess.engine.UciProtocol:
        """Launch a fresh process and apply per-engine + global + override options.

        Override precedence (later wins): per-engine options -> global_defaults
        -> overrides. Unknown / managed options are skipped, not raised.
        """
        transport, engine = await self._open_uci()
        self._transport = transport
        rc_future = getattr(engine, "returncode", None)
        if rc_future is not None:
            rc_future.add_done_callback(lambda f: f.exception())
        self._patch_log(engine)
        accepted: dict = {}
        for k, v in (self._options or {}).items():
            if k in engine.options and not engine.options[k].is_managed():
                accepted[k] = v
            else:
                log.warning(
                    "engine %s: skipping unknown/managed option %s",
                    self._engine_path, k,
                )
        for k, v in (global_defaults or {}).items():
            if k in engine.options and not engine.options[k].is_managed():
                accepted[k] = v
        for k, v in (overrides or {}).items():
            if k in engine.options and not engine.options[k].is_managed():
                accepted[k] = v
        if accepted:
            try:
                await engine.configure(accepted)
            except chess.engine.EngineError:
                log.error("engine refused options %s", accepted, exc_info=True)
        return engine

    async def spawn_throwaway(
        self,
        overrides: dict | None = None,
        global_defaults: dict | None = None,
    ) -> tuple[chess.engine.UciProtocol, Callable[[], Awaitable[None]]]:
        """Spawn a one-shot engine and return (engine, async cleanup).

        Unlike ``spawn()`` which is paired with the long-lived
        self._engine / self._transport state used by ``ensure()`` /
        ``quit()``, this entry point is for short-lived analyses (e.g.
        the AI agent's `analyze` tool) that own their own engine for
        the duration of a single search and tear it down on exit.

        The returned cleanup is an async callable -- ``await cleanup()``
        -- that sends `quit`, closes all pipe transports, and awaits
        their connection_lost callbacks. Calling it is mandatory; not
        calling it leaks the asyncio subprocess transport (Windows GC
        then fires ResourceWarning).
        """
        transport, engine = await self._open_uci()
        rc_future = getattr(engine, "returncode", None)
        if rc_future is not None:
            rc_future.add_done_callback(lambda f: f.exception())
        self._patch_log(engine)
        accepted: dict = {}
        for k, v in (self._options or {}).items():
            if k in engine.options and not engine.options[k].is_managed():
                accepted[k] = v
        for k, v in (global_defaults or {}).items():
            if k in engine.options and not engine.options[k].is_managed():
                accepted[k] = v
        for k, v in (overrides or {}).items():
            if k in engine.options and not engine.options[k].is_managed():
                accepted[k] = v
        if accepted:
            try:
                await engine.configure(accepted)
            except chess.engine.EngineError:
                log.error("engine refused options %s", accepted, exc_info=True)

        async def _cleanup() -> None:
            """Exception-safe by contract: callers don't wrap. Swallows
            quit failures, pipe-close failures (incl. Windows-proactor
            OSError), and a hung asyncio.wait timeout."""
            try:
                await engine.quit()
            except (chess.engine.EngineTerminatedError, RuntimeError, BrokenPipeError):
                pass
            waiters: list[asyncio.Future] = []
            for fd in (0, 1, 2):
                try:
                    pipe = transport.get_pipe_transport(fd)
                except Exception:
                    pipe = None
                if pipe is not None:
                    waiters.append(_close_and_wait(pipe))
            waiters.append(_close_and_wait(transport))
            try:
                await asyncio.wait(waiters, timeout=_CLOSE_GRACE_SECONDS)
            except Exception:
                log.error("engine cleanup: transport close raised", exc_info=True)

        return engine, _cleanup

    async def ensure(
        self,
        global_defaults: dict | None = None,
    ) -> chess.engine.UciProtocol:
        """Return the long-lived engine, spawning on first call.

        Fills engine_name from the UCI `id name` on first spawn, falling back
        to the binary basename. An explicit engine_name (set by the API layer)
        is preserved.
        """
        if self._engine is None:
            self._engine = await self.spawn(global_defaults=global_defaults)
            if not self._engine_name:
                self._engine_name = (
                    self._engine.id.get("name") or Path(self._engine_path).name
                )
        return self._engine

    async def cancel(
        self,
        think_task: asyncio.Task | None,
        analysis: Any | None,
    ) -> None:
        """Stop the active search. Keep the engine alive for reuse unless the
        engine fails to acknowledge `stop` within the grace period, in which
        case tear down the transport so the next ensure() respawns.

        Caller owns think_task and analysis (HVE retains them for the search
        loop). Caller must clear its own references before/after this call;
        the supervisor only touches transport/engine state.
        """
        if think_task is None or think_task.done():
            return
        if analysis is not None:
            try:
                analysis.stop()
            except Exception as e:
                log.warning("failed to stop analysis: %s", e)
        elif self._engine is not None:
            try:
                self._engine.send_line("stop")
            except Exception as e:
                log.warning("failed to send stop to engine: %s", e)
        try:
            await asyncio.wait_for(
                asyncio.shield(think_task), timeout=_CANCEL_GRACE_SECONDS,
            )
            log.info("engine responded to stop")
        except asyncio.TimeoutError:
            log.warning("engine did not respond to stop -- terminating")
            transport = getattr(self._engine, "transport", None)
            if transport is not None:
                try:
                    transport.close()
                except (BrokenPipeError, OSError) as e:
                    log.warning("failed to close engine transport: %s", e)
            self._engine = None
        except (asyncio.CancelledError, Exception):
            pass
        think_task.cancel()

    async def quit(self) -> None:
        """Gracefully terminate the engine subprocess. Idempotent.

        python-chess's UciProtocol.quit() only sends ``quit`` and awaits
        the returncode; it never closes the outer subprocess transport
        or its child stdin/stdout/stderr pipes. On Windows that leaves
        pipe transports whose GC trips ResourceWarning later. Close them
        explicitly and await each transport's connection_lost so the
        proactor's close callbacks complete before we return."""
        if self._engine is not None:
            try:
                await self._engine.quit()
            except (chess.engine.EngineTerminatedError, RuntimeError, BrokenPipeError):
                pass
            self._engine = None
        if self._transport is not None:
            waiters: list[asyncio.Future] = []
            for fd in (0, 1, 2):
                try:
                    pipe = self._transport.get_pipe_transport(fd)
                except Exception:
                    pipe = None
                if pipe is not None:
                    waiters.append(_close_and_wait(pipe))
            waiters.append(_close_and_wait(self._transport))
            self._transport = None
            # Bound the wait so a wedged proactor cannot hang shutdown
            # forever; the timeout is a safety net, not the sync primitive.
            await asyncio.wait(waiters, timeout=_CLOSE_GRACE_SECONDS)
        self._uci_log_tasks.clear()

    async def swap(self, path: str) -> None:
        """Replace the engine binary; clear per-engine config so the new
        binary's options/args/env don't inherit stale state. Caller must
        cancel any active search before invoking."""
        await self.quit()
        self._engine_path = path
        self._engine_name = None
        self._options = {}
        self._args = []
        self._env = {}

    async def apply_settings_live(self) -> None:
        """Discard the live engine so the next ensure() respawns with current
        options/args/env."""
        await self.quit()

    # ------------------------------------------------------------------
    # UCI log fanout
    # ------------------------------------------------------------------

    def _patch_log(self, engine: chess.engine.UciProtocol) -> None:
        """Wrap send_line / line_received to emit uci_log events.

        The uci/uciok handshake done by popen_uci runs before this patch,
        so those lines are not captured. setoption + isready and all
        subsequent traffic are.
        """
        bus = self._bus
        tasks = self._uci_log_tasks
        loop = asyncio.get_running_loop()
        orig_send = engine.send_line
        orig_recv = engine.line_received

        def _emit(direction: str, line: str) -> None:
            t = loop.create_task(
                bus.publish(Event(kind=EVT_UCI_LOG, payload={"dir": direction, "line": line}))
            )
            tasks.add(t)
            t.add_done_callback(tasks.discard)

        def _send(line: str) -> None:
            orig_send(line)
            _emit(">", line)

        def _recv(line: str) -> None:
            orig_recv(line)
            _emit("<", line)

        engine.send_line = _send
        engine.line_received = _recv
