"""PyWebView wrapper. Runs uvicorn in a background thread, then opens a native window."""
from __future__ import annotations

import errno
import logging
import os
import socket
import threading
from html import escape as html_escape
from pathlib import Path

import uvicorn
from platformdirs import user_data_dir

from . import app_dir_name
from ._uvicorn_signal import make_signalling_server
from .app import create_app
from .config import Settings

_SERVER_STARTUP_TIMEOUT = 5.0
_SERVER_SHUTDOWN_TIMEOUT = 5.0
_MIN_WINDOW_WIDTH = 960
_MIN_WINDOW_HEIGHT = 720
_STARTUP_ERROR_TITLE = "sturddle-view could not start"
_PORT_IN_USE_MESSAGE = (
    "Port {port} is already in use -- another copy may be running. "
    "Close it, or start on a different port: sturddle-view --port {alt}"
)
_STARTUP_FAILED_MESSAGE = (
    "The local server exited during startup. Another copy may be running, "
    "or port {port} may be blocked. Try a different port: "
    "sturddle-view --port {alt}"
)
_STARTUP_TIMEOUT_MESSAGE = (
    "The local server did not start within {timeout:.0f}s on port {port}."
)
_PGN_FILE_TYPES = ("PGN (*.pgn)", "All files (*.*)")
_CLOSE_CONFIRM_TITLE = "Tournament in progress"
_CLOSE_CONFIRM_MESSAGE = (
    "A tournament is currently running ({name}). "
    "Closing now will stop it; on restart it will start from scratch and "
    "all recorded games will be discarded.\n\nClose anyway?"
)

log = logging.getLogger(__name__)


class JsApi:
    """Bridge exposed to the embedded page as ``window.pywebview.api``.

    WebView2 ignores HTML5 download attributes, so the browser-side
    ``a.download`` save trick is a no-op in desktop mode. Routes that
    need to write to the user's local filesystem go through this bridge
    instead, which uses PyWebView's native file dialog.
    """

    def __init__(self, save_dialog_kind: object | None = None) -> None:
        self._window = None
        self._save_dialog_kind = save_dialog_kind

    def attach(self, window) -> None:
        self._window = window

    def save_pgn(self, pgn_text: str, default_filename: str) -> dict:
        window = self._window
        if window is None:
            return {"ok": False, "error": "window not attached"}
        try:
            result = window.create_file_dialog(
                self._save_dialog_kind,
                save_filename=default_filename,
                file_types=_PGN_FILE_TYPES,
            )
        except Exception as exc:
            log.error("save_pgn: dialog failed", exc_info=True)
            return {"ok": False, "error": str(exc)}
        if not result:
            return {"ok": False, "cancelled": True}
        # PyWebView returns either a string or a sequence depending on
        # backend; normalize.
        path = result[0] if isinstance(result, (list, tuple)) else result
        try:
            Path(path).write_text(pgn_text, encoding="utf-8", newline="")
        except OSError as exc:
            log.error("save_pgn: write failed for %s", path, exc_info=True)
            return {"ok": False, "error": str(exc), "path": str(path)}
        return {"ok": True, "path": str(path)}


def _port_in_use(host: str, port: int) -> bool:
    """True iff binding (host, port) fails with address-in-use/permission.
    A pre-flight check: uvicorn swallows the bind OSError and sys.exit(1)s,
    so we detect the common case ourselves to show a useful message. errno
    is portable (EADDRINUSE on POSIX and Windows/WSAEADDRINUSE alike).

    SO_REUSEADDR matches uvicorn's listener so a port in TIME_WAIT isn't
    misread as in-use -- but only off Windows, where REUSEADDR instead lets
    a bind steal an actively-listening port (which would misread it free)."""
    bind_host = "127.0.0.1" if host == "0.0.0.0" else host
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((bind_host, port))
        return False
    except OSError as exc:
        return exc.errno in (errno.EADDRINUSE, errno.EACCES)
    finally:
        probe.close()


def show_error(title: str, message: str) -> None:
    try:
        import webview  # type: ignore[import-untyped]
        # Escape first, then turn newlines into breaks: messages can carry a
        # raw exception string, so never interpolate it unescaped into HTML.
        safe = html_escape(message).replace("\n", "<br>")
        html = (
            "<html><head><style>"
            "body{margin:0;display:flex;align-items:center;justify-content:center;"
            "height:100vh;font-family:system-ui,sans-serif;background:#1e1e1e;color:#ccc;}"
            "p{text-align:center;font-size:14px;padding:0 24px;line-height:1.5;}"
            "</style></head><body><p>" + safe + "</p></body></html>"
        )
        w = webview.create_window(title, html=html, width=460, height=160)
        webview.start()
    except Exception:
        pass


def _active_tournament_name(app) -> str | None:
    """Return the running tournament's name, or None. Defensive: any
    lookup failure (orch unattached, store error) returns None so the
    close handler never blocks on bookkeeping bugs."""
    try:
        orch = getattr(app.state, "tournament_orch", None)
        if orch is None:
            return None
        active_id = orch.active_id()
        if not active_id:
            return None
        store = getattr(app.state, "tournament_store", None)
        if store is None:
            return None
        return store.get(active_id).name
    except Exception:
        log.error("close-confirm: active tournament lookup failed", exc_info=True)
        return None


def _make_close_handler(app, window):
    def on_closing() -> bool | None:
        # Return False to cancel the close. PyWebView treats any False in
        # the handler return set as "abort"; None / True allow the close.
        name = _active_tournament_name(app)
        if not name:
            return None
        confirmed = window.create_confirmation_dialog(
            _CLOSE_CONFIRM_TITLE, _CLOSE_CONFIRM_MESSAGE.format(name=name),
        )
        return None if confirmed else False
    return on_closing


def run_desktop(host: str, port: int, width: int = 1280, height: int = 800) -> None:
    try:
        import webview  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit("PyWebView is not installed. Install with: pip install '.[desktop]'") from exc

    # Pre-flight the port: uvicorn catches the bind OSError and sys.exit(1)s,
    # which surfaces as an unhelpful SystemExit(1). Detect the common case
    # here so the user gets a clear "port in use" window with guidance.
    if _port_in_use(host, port):
        show_error(
            _STARTUP_ERROR_TITLE, _PORT_IN_USE_MESSAGE.format(port=port, alt=port + 1)
        )
        raise SystemExit(f"port {port} already in use")

    settings = Settings(host=host, port=port)
    # Pin the token so create_app picks up the same value via Settings()
    # (rather than rolling a new random one) -- the /auth handshake below
    # validates against this token.
    os.environ.setdefault("SV_TOKEN", settings.token)

    # Build the app eagerly so we can hand the same instance to uvicorn
    # AND keep a reference for the close-confirm handler (reads
    # app.state.tournament_orch to decide whether to prompt).
    app = create_app(settings=settings)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
    )

    server, signal = make_signalling_server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for either outcome. A bind failure (port in use) sets done with
    # an error; a slow/stuck start trips the timeout. Both must surface a
    # window with guidance instead of exiting to a blank screen.
    if not signal.done.wait(timeout=_SERVER_STARTUP_TIMEOUT):
        server.should_exit = True
        thread.join(timeout=_SERVER_SHUTDOWN_TIMEOUT)
        show_error(
            _STARTUP_ERROR_TITLE,
            _STARTUP_TIMEOUT_MESSAGE.format(
                timeout=_SERVER_STARTUP_TIMEOUT, port=port,
            ),
        )
        raise SystemExit("server did not start within timeout")
    if signal.error is not None:
        # Pre-flight already caught port-in-use; anything reaching here is
        # another startup failure (uvicorn turns the bind error into a bare
        # SystemExit(1), so its str is useless -- give port guidance anyway).
        server.should_exit = True
        thread.join(timeout=_SERVER_SHUTDOWN_TIMEOUT)
        log.error("desktop server startup failed: %r", signal.error)
        show_error(
            _STARTUP_ERROR_TITLE, _STARTUP_FAILED_MESSAGE.format(port=port, alt=port + 1)
        )
        raise SystemExit(f"server startup failed: {signal.error!r}")

    window_host = "127.0.0.1" if host == "0.0.0.0" else host
    # Use the cookie handshake: /auth validates the token, sets an HttpOnly
    # cookie, then 303s to /ui/. The window's history never holds the token.
    url = f"http://{window_host}:{port}/auth?token={settings.token}"
    api = JsApi(save_dialog_kind=webview.FileDialog.SAVE)
    window = webview.create_window(
        "sturddle-view", url, width=width, height=height,
        min_size=(_MIN_WINDOW_WIDTH, _MIN_WINDOW_HEIGHT), js_api=api,
    )
    api.attach(window)
    window.events.closing += _make_close_handler(app, window)
    webview.start(private_mode=False, storage_path=user_data_dir(app_dir_name(), appauthor=False))

    server.should_exit = True
    thread.join(timeout=_SERVER_SHUTDOWN_TIMEOUT)
