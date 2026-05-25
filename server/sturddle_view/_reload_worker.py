from __future__ import annotations

import asyncio
import os


def windows_reload_worker(sockets):  # noqa: ARG001 -- signature dictated by uvicorn
    # Lives in a regular module (not __main__) so spawn can pickle it.
    # Ignores parent sockets; binds fresh in-child to avoid the IOCP
    # re-registration crash (WinError 87) on subsequent reloads.
    from uvicorn import Config, Server

    config = Config(
        "sturddle_view.app:create_app",
        host=os.environ["SV_HOST"],
        port=int(os.environ["SV_PORT"]),
        reload=True,
        loop=asyncio.ProactorEventLoop,
        factory=True,
        log_config=None,
        access_log=False,
        ssl_certfile=os.environ.get("SV_TLS_CERT") or None,
        ssl_keyfile=os.environ.get("SV_TLS_KEY") or None,
    )
    Server(config=config).run(sockets=None)
