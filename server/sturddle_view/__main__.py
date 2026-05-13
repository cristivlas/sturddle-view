from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn
from platformdirs import user_config_dir

from . import APP_NAME
from ._instance_lock import acquire as _acquire_lock
from .config import Settings
from .logging_setup import configure_logging


def main() -> None:
    # "proxy" subcommand: the frozen exe re-invokes itself to run the stdio
    # proxy (see _runtime.proxy_argv_prefix). Strip the subcommand token so
    # proxy.main() receives a clean argv.
    if sys.argv[1:2] == ["proxy"]:
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .tournament.proxy import main as _proxy_main
        _proxy_main()
        return

    parser = argparse.ArgumentParser(prog="sturddle-view")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--desktop", action="store_true", help="Open in PyWebView native window")
    parser.add_argument("--width", type=int, default=1280, help="Desktop window width (default 1280)")
    parser.add_argument("--height", type=int, default=800, help="Desktop window height (default 800)")
    parser.add_argument("--reload", action="store_true", help="Dev mode: auto-reload on changes")
    parser.add_argument("--engine", default=None, help="Path to UCI engine binary")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable token auth (dev convenience; do not use on untrusted networks)",
    )
    parser.add_argument("--debug", action="store_true",
                        help="Verbose (DEBUG) logging for the app (sturddle_view)")
    parser.add_argument("--server-debug", action="store_true",
                        help="Verbose (DEBUG) logging for uvicorn (independent of --debug)")
    args = parser.parse_args()

    log_file = configure_logging(
        level=logging.DEBUG if args.debug else logging.INFO,
        server_level=logging.DEBUG if args.server_debug else logging.WARNING,
    )
    logging.getLogger(__name__).info("logging to %s", log_file)

    if not args.reload:
        lock_path = Path(user_config_dir(APP_NAME, appauthor=False)) / "server.lock"
        if not _acquire_lock(lock_path):
            logging.getLogger(__name__).error(
                "Another %s instance is already running. Exiting.", APP_NAME
            )
            sys.exit(1)

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

        run_desktop(host=host, port=port, width=args.width, height=args.height)
        return

    # On Windows, uvicorn's reload mode forces SelectorEventLoop in the worker,
    # which cannot spawn subprocesses (asyncio raises NotImplementedError from
    # _make_subprocess_transport). We need ProactorEventLoop to launch UCI engines.
    loop: object = "auto"
    if sys.platform == "win32" and args.reload:
        loop = asyncio.ProactorEventLoop

    # Windows: SIGINT can't preempt uvicorn's C-level blocking; use the
    # OS console handler instead. Hard-exits — children rely on Job Object
    # (TODO 2) for cleanup.
    if sys.platform == "win32" and not args.reload:
        _install_windows_ctrl_handler()

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
        access_log=False,
    )


_console_handler_ref = None  # keep wrapper alive across the OS callback


def _install_windows_ctrl_handler() -> None:
    """Force-exit on Ctrl+C / Break / console close. The OS calls this
    handler on a dedicated thread, bypassing Python's signal queue."""
    import ctypes
    global _console_handler_ref
    HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    # CTRL_C_EVENT=0, CTRL_BREAK_EVENT=1, CTRL_CLOSE_EVENT=2,
    # CTRL_LOGOFF_EVENT=5, CTRL_SHUTDOWN_EVENT=6.
    _HANDLED = {0, 1, 2, 5, 6}

    def _handler(ctrl_type: int) -> bool:
        if ctrl_type in _HANDLED:
            os._exit(130)
        return False

    _console_handler_ref = HANDLER(_handler)
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref, True):
        logging.getLogger(__name__).warning(
            "SetConsoleCtrlHandler failed; Ctrl+C may not exit cleanly."
        )


if __name__ == "__main__":
    main()
