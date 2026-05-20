"""Uvicorn Server subclass that signals startup completion via threading.Event."""
from __future__ import annotations

import threading

import uvicorn


def make_signalling_server(config: uvicorn.Config) -> tuple[uvicorn.Server, threading.Event]:
    """Return (server, started_event). The event fires after the server's
    ``startup()`` completes on its own loop -- a real readiness signal,
    not a polled flag."""
    started = threading.Event()

    class _SignallingServer(uvicorn.Server):
        async def startup(self, sockets=None):  # type: ignore[override]
            await super().startup(sockets=sockets)
            started.set()

    return _SignallingServer(config), started
