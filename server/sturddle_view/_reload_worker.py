from __future__ import annotations

import asyncio
import os

from uvicorn import Config, Server

from .config import DEFAULT_PORT, ENV_HOST, ENV_PORT, ENV_TLS_CERT, ENV_TLS_KEY, LOOPBACK_HOST
from .env_utils import env_int

# uvicorn import string for the app factory (``factory=True``).
APP_FACTORY = "sturddle_view.app:create_app"


def windows_reload_worker(sockets):  # noqa: ARG001 -- signature dictated by uvicorn
    # Lives in a regular module (not __main__) so spawn can pickle it.
    # Ignores parent sockets; binds fresh in-child to avoid the IOCP
    # re-registration crash (WinError 87) on subsequent reloads.
    # The parent hands host/port/TLS over through the environment.
    config = Config(
        APP_FACTORY,
        host=os.environ.get(ENV_HOST, LOOPBACK_HOST),
        port=env_int(ENV_PORT, DEFAULT_PORT),
        reload=True,
        loop=asyncio.ProactorEventLoop,
        factory=True,
        log_config=None,
        access_log=False,
        ssl_certfile=os.environ.get(ENV_TLS_CERT) or None,
        ssl_keyfile=os.environ.get(ENV_TLS_KEY) or None,
    )
    Server(config=config).run(sockets=None)
