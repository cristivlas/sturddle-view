"""Uvicorn Server subclasses that signal their startup outcome: to the
launcher thread (threading.Event), or to the test harness (SV_READY_PORT)."""
from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field

import uvicorn

from .config import LOOPBACK_HOST
from .env_utils import env_int

_READY_PORT_ENV = "SV_READY_PORT"
# SV_READY_PORT unset: not launched by the test harness.
_NO_READY_PORT = 0
_READY_SIGNAL_TIMEOUT_S = 2.0


@dataclass
class StartupSignal:
    """Startup outcome shared with the launcher thread. `ready` fires on a
    clean startup; `done` fires on either outcome so the caller can wait
    once and branch. `error` holds the startup exception (e.g. the port was
    already bound) when startup failed -- it would otherwise die unseen in
    the daemon thread."""
    ready: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


def make_signalling_server(config: uvicorn.Config) -> tuple[uvicorn.Server, StartupSignal]:
    """Return (server, signal). `signal.ready` fires after the server's
    ``startup()`` completes; `signal.done` fires on success or failure, with
    `signal.error` set on failure -- a real readiness/failure signal, not a
    polled flag. Run the server with ``server.run`` on a thread; a bind
    failure (port in use) sets error+done instead of vanishing."""
    signal = StartupSignal()

    class _SignallingServer(uvicorn.Server):
        async def startup(self, sockets=None):  # type: ignore[override]
            try:
                await super().startup(sockets=sockets)
            except BaseException as exc:  # bind failure, etc.
                signal.error = exc
                signal.done.set()
                raise
            signal.ready.set()
            signal.done.set()

    return _SignallingServer(config), signal


def _signal_ready_port() -> None:
    """Connect once to SV_READY_PORT, the harness's listening socket. Silent
    no-op when the env var is unset or unreachable."""
    port = env_int(_READY_PORT_ENV, _NO_READY_PORT, min_value=1)
    if port == _NO_READY_PORT:
        return
    try:
        socket.create_connection((LOOPBACK_HOST, port), timeout=_READY_SIGNAL_TIMEOUT_S).close()
    except OSError:
        pass


class ReadyPortServer(uvicorn.Server):
    """Signals SV_READY_PORT once startup() has bound the listen sockets, so
    the harness's ready event means connections are accepted. Lifespan
    startup runs before the bind; a failed startup exits instead."""

    async def startup(self, sockets=None):  # type: ignore[override]
        await super().startup(sockets=sockets)
        _signal_ready_port()
