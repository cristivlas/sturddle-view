from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from .config import Settings
from .logging_setup import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="sturddle-view")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--desktop", action="store_true", help="Open in PyWebView native window")
    parser.add_argument("--reload", action="store_true", help="Dev mode: auto-reload on changes")
    parser.add_argument("--engine", default=None, help="Path to UCI engine binary")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable token auth (dev convenience; do not use on untrusted networks)",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose (DEBUG) logging")
    args = parser.parse_args()

    log_file = configure_logging(level=logging.DEBUG if args.debug else logging.INFO)
    logging.getLogger(__name__).info("logging to %s", log_file)

    # Push CLI overrides into env so the worker process's Settings() picks them up.
    if args.engine:
        os.environ["STURDDLE_ENGINE_PATH"] = args.engine
    if args.host:
        os.environ["STURDDLE_HOST"] = args.host
    if args.port:
        os.environ["STURDDLE_PORT"] = str(args.port)
    if args.no_auth:
        os.environ["STURDDLE_AUTH_DISABLED"] = "1"

    settings = Settings()
    host = settings.host
    port = settings.port

    if args.desktop:
        from .desktop import run_desktop

        run_desktop(host=host, port=port)
        return

    # On Windows, uvicorn's reload mode forces SelectorEventLoop in the worker,
    # which cannot spawn subprocesses (asyncio raises NotImplementedError from
    # _make_subprocess_transport). We need ProactorEventLoop to launch UCI engines.
    loop: object = "auto"
    if sys.platform == "win32" and args.reload:
        loop = asyncio.ProactorEventLoop

    uvicorn.run(
        "sturddle_view.app:create_app",
        host=host,
        port=port,
        reload=args.reload,
        loop=loop,
        factory=True,
        # Use the project's logging config (configure_logging above), not
        # uvicorn's default — which would otherwise overwrite our settings
        # and re-enable per-request access lines we already silenced.
        log_config=None,
    )


if __name__ == "__main__":
    main()
