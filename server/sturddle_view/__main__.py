from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import os
import sys
from pathlib import Path
from typing import NoReturn

import uvicorn
from uvicorn import Config
from uvicorn.supervisors import ChangeReload

from . import APP_NAME, INSTANCE_ENV, app_config_dir, is_windows
from ._instance_lock import acquire as _acquire_lock
from ._reload_worker import APP_FACTORY, windows_reload_worker
from ._runtime import DESKTOP_FLAG, PROXY_SUBCOMMAND, is_frozen
from .auth import AUTH_DISABLED_LAN_WARNING
from .config import (
    ENV_AUTH_DISABLED,
    ENV_ENGINE_PATH,
    ENV_HOST,
    ENV_PORT,
    ENV_TLS_CERT,
    ENV_TLS_KEY,
    LOOPBACK_HOST,
    Settings,
)
from .env_utils import env_path
from .logging_setup import configure_logging
from .tournament.proxy import main as _proxy_main

log = logging.getLogger(__name__)

_ALREADY_RUNNING_DETAILS = (
    f"Every {APP_NAME} process stores its settings and game data in the "
    "same on-disk folder, so only one instance may run at a time. To start "
    "another instance side by side, give it its own storage: "
    f"`{APP_NAME} --instance <name>` (add `--port <n>` if the port is taken too)."
)

_DEFAULT_WINDOW_WIDTH = 1280
_DEFAULT_WINDOW_HEIGHT = 1000
_STORE_TRUE = "store_true"
_CERT_FLAG = "--cert"
_KEY_FLAG = "--key"
_LOCK_FILENAME = "server.lock"
_ENV_TRUE = "1"

# Process exit codes: argparse's usage-error code, a generic failure, and
# the shell convention for death by SIGINT (128 + 2).
_EXIT_USAGE = 2
_EXIT_FAILURE = 1
_EXIT_INTERRUPTED = 130

# Console control events the Windows handler turns into a hard exit.
_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1
_CTRL_CLOSE_EVENT = 2
_CTRL_LOGOFF_EVENT = 5
_CTRL_SHUTDOWN_EVENT = 6
_HANDLED_CTRL_EVENTS = frozenset({
    _CTRL_C_EVENT, _CTRL_BREAK_EVENT, _CTRL_CLOSE_EVENT,
    _CTRL_LOGOFF_EVENT, _CTRL_SHUTDOWN_EVENT,
})


def _desktop():
    # Deferred: the frozen exe also runs every per-engine stdio proxy through
    # main(), and importing the app + PyWebView would slow each proxy spawn.
    from . import desktop
    return desktop


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(DESKTOP_FLAG, action=_STORE_TRUE, default=is_frozen(),
                        help="Open in PyWebView native window (default in the frozen exe)")
    parser.add_argument("--width", type=int, default=_DEFAULT_WINDOW_WIDTH,
                        help="Desktop window width (default %(default)s)")
    parser.add_argument("--height", type=int, default=_DEFAULT_WINDOW_HEIGHT,
                        help="Desktop window height (default %(default)s)")
    parser.add_argument("--reload", action=_STORE_TRUE, help="Dev mode: auto-reload on changes")
    parser.add_argument("--engine", default=None, help="Path to UCI engine binary")
    parser.add_argument(
        "--no-auth",
        action=_STORE_TRUE,
        help="Disable token auth (dev convenience; do not use on untrusted networks)",
    )
    parser.add_argument(_CERT_FLAG, default=None, help=f"TLS cert (PEM). Requires {_KEY_FLAG}.")
    parser.add_argument(_KEY_FLAG, default=None, help=f"TLS key (PEM). Requires {_CERT_FLAG}.")
    parser.add_argument(
        "--instance",
        default=None,
        metavar="TAG",
        help="Instance tag (e.g. 2, beta) -- isolates config/data dirs so multiple instances can run side-by-side",
    )
    parser.add_argument("--debug", action=_STORE_TRUE,
                        help="Verbose (DEBUG) logging for the app (sturddle_view)")
    parser.add_argument("--server-debug", action=_STORE_TRUE,
                        help="Verbose (DEBUG) logging for uvicorn (independent of --debug)")
    return parser


def _usage_error(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(_EXIT_USAGE)


def main() -> None:
    # "proxy" subcommand: the frozen exe re-invokes itself to run the stdio
    # proxy (see _runtime.proxy_argv_prefix). Strip the subcommand token so
    # proxy.main() receives a clean argv. Dispatched before any parsing so
    # server-only flags (e.g. --desktop) can never leak into the proxy chain.
    if sys.argv[1:2] == [PROXY_SUBCOMMAND]:
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        _proxy_main()
        return

    args = _build_parser().parse_args()

    # Validate CLI combos before any side effects (logging dir, lockfile,
    # env mutations). Bad flags should exit cleanly without touching state.
    if (args.cert is None) != (args.key is None):
        _usage_error(f"{_CERT_FLAG} and {_KEY_FLAG} must be provided together.")
    if args.cert and args.desktop:
        _usage_error(f"{_CERT_FLAG}/{_KEY_FLAG} are not supported with {DESKTOP_FLAG} "
                     "(loopback HTTP is already a secure context).")
    if args.cert:
        for label, p in ((_CERT_FLAG, args.cert), (_KEY_FLAG, args.key)):
            if not Path(p).is_file():
                _usage_error(f"{label} file not found: {p}")

    if args.instance:
        os.environ[INSTANCE_ENV] = args.instance.strip()

    log_file = configure_logging(
        level=logging.DEBUG if args.debug else logging.INFO,
        server_level=logging.DEBUG if args.server_debug else logging.WARNING,
    )
    log.info("logging to %s", log_file)

    if not args.reload:
        # ``SV_INSTANCE_LOCK_PATH`` overrides the default lock location
        # (used by tests to give each subprocess its own lock).
        lock_path = env_path("SV_INSTANCE_LOCK_PATH", app_config_dir() / _LOCK_FILENAME)
        if not _acquire_lock(lock_path):
            msg = f"Another {APP_NAME} instance is already running."
            log.error("%s Exiting.", msg)
            print(msg, file=sys.stderr)
            if args.desktop:
                _desktop().show_error(APP_NAME, msg, details=_ALREADY_RUNNING_DETAILS)
            sys.exit(_EXIT_FAILURE)

    # Push CLI overrides into env so the worker process's Settings() picks them up.
    if args.engine:
        os.environ[ENV_ENGINE_PATH] = args.engine
    if args.host:
        os.environ[ENV_HOST] = args.host
    if args.port:
        os.environ[ENV_PORT] = str(args.port)
    if args.no_auth:
        os.environ[ENV_AUTH_DISABLED] = _ENV_TRUE
    if args.cert:
        os.environ[ENV_TLS_CERT] = args.cert
        os.environ[ENV_TLS_KEY] = args.key

    settings = Settings()
    host = settings.host
    port = settings.port

    if args.no_auth and host != LOOPBACK_HOST:
        # Explicit, intentional combo: warn loudly but allow (tailscale / trusted LAN).
        log.warning(AUTH_DISABLED_LAN_WARNING, host)

    if args.desktop:
        _desktop().run_desktop(host=host, port=port, width=args.width, height=args.height)
        return

    # On Windows, uvicorn's reload mode forces SelectorEventLoop in the worker,
    # which cannot spawn subprocesses (asyncio raises NotImplementedError from
    # _make_subprocess_transport). We need ProactorEventLoop to launch UCI engines.
    loop: object = "auto"
    if is_windows() and args.reload:
        loop = asyncio.ProactorEventLoop

    # Windows: SIGINT can't preempt uvicorn's C-level blocking; use the
    # OS console handler instead. Hard-exits -- children rely on the
    # tournament Job Object for cleanup.
    if is_windows() and not args.reload:
        _install_windows_ctrl_handler()

    if is_windows() and args.reload:
        _run_windows_reload(
            host=host, port=port, loop=loop,
            ssl_certfile=args.cert, ssl_keyfile=args.key,
        )
        return

    uvicorn.run(
        APP_FACTORY,
        host=host,
        port=port,
        reload=args.reload,
        loop=loop,
        factory=True,
        # Use the project's logging config (configure_logging above), not
        # uvicorn's default -- which would otherwise overwrite our settings
        # and re-enable per-request access lines we already silenced.
        log_config=None,
        access_log=False,
        ssl_certfile=args.cert,
        ssl_keyfile=args.key,
    )


def _run_windows_reload(
    *,
    host: str,
    port: int,
    loop: object,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
) -> None:
    # uvicorn.run pre-binds in the parent and reuses that socket across
    # worker restarts. On Windows the 2nd worker's CreateIoCompletionPort on
    # the inherited socket fails with WinError 87. Each worker binds fresh.
    os.environ[ENV_HOST] = host
    os.environ[ENV_PORT] = str(port)
    if ssl_certfile:
        os.environ[ENV_TLS_CERT] = ssl_certfile
    if ssl_keyfile:
        os.environ[ENV_TLS_KEY] = ssl_keyfile

    parent_config = Config(
        APP_FACTORY,
        host=host,
        port=port,
        reload=True,
        loop=loop,
        factory=True,
        log_config=None,
        access_log=False,
    )
    ChangeReload(parent_config, target=windows_reload_worker, sockets=[]).run()


_console_handler_ref = None  # keep wrapper alive across the OS callback


def _install_windows_ctrl_handler() -> None:
    """Force-exit on Ctrl+C / Break / console close. The OS calls this
    handler on a dedicated thread, bypassing Python's signal queue."""
    global _console_handler_ref
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    def _handler(ctrl_type: int) -> bool:
        if ctrl_type in _HANDLED_CTRL_EVENTS:
            os._exit(_EXIT_INTERRUPTED)
        return False

    _console_handler_ref = handler_type(_handler)
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref, True):
        log.warning("SetConsoleCtrlHandler failed; Ctrl+C may not exit cleanly.")


if __name__ == "__main__":
    main()
