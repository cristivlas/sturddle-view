"""Uvicorn Server subclass that signals startup outcome via threading.Event."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

import uvicorn


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
