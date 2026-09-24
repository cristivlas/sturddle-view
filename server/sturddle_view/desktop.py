"""PyWebView wrapper. Runs uvicorn in a background thread, then opens a native window."""
from __future__ import annotations

import ctypes
import errno
import logging
import os
import re
import socket
import threading
from html import escape as html_escape
from pathlib import Path

import uvicorn

try:
    import webview  # type: ignore[import-untyped]
except ImportError:  # optional: installed with the [desktop] extra
    webview = None

from . import APP_NAME, app_data_dir, is_windows
from ._uvicorn_signal import make_signalling_server
from .app import create_app
from .config import ENV_TOKEN, LOOPBACK_HOST, WEB_DIR, WILDCARD_HOST, Settings
from .env_utils import env_float
from .error_detail import ERROR_KEY
from .lan_listener import LanListener
from .netinfo import entry_url

_SERVER_STARTUP_TIMEOUT = env_float("SV_DESKTOP_STARTUP_TIMEOUT_S", 5.0, min_value=0.0)
_SERVER_SHUTDOWN_TIMEOUT = env_float("SV_DESKTOP_SHUTDOWN_TIMEOUT_S", 5.0, min_value=0.0)
_MIN_WINDOW_WIDTH = 960
_MIN_WINDOW_HEIGHT = 720
_STARTUP_ERROR_TITLE = f"{APP_NAME} could not start"
_PORT_IN_USE_MESSAGE = (
    "Port {port} is already in use -- another copy may be running. "
    f"Close it, or start on a different port: {APP_NAME} --port {{alt}}"
)
_STARTUP_FAILED_MESSAGE = (
    "The local server exited during startup. Another copy may be running, "
    "or port {port} may be blocked. Try a different port: "
    f"{APP_NAME} --port {{alt}}"
)
_STARTUP_TIMEOUT_MESSAGE = (
    "The local server did not start within {timeout:.0f}s on port {port}."
)
_WINDOW_ICON = WEB_DIR / "app.ico"
# Win32 constants for WM_SETICON (see _apply_window_icon).
_WM_SETICON = 0x0080
_ICON_SMALL, _ICON_BIG = 0, 1
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_LR_DEFAULTSIZE = 0x0040
_ERROR_WINDOW_WIDTH = 460
_ERROR_WINDOW_HEIGHT = 160
_ERROR_WINDOW_HEIGHT_DETAILS = 300
_ERROR_DETAILS_SUMMARY = "Why am I seeing this?"
_PGN_FILE_TYPES = ("PGN (*.pgn)", "All files (*.*)")
_CLOSE_CONFIRM_TITLE = "Tournament in progress"
_CLOSE_CONFIRM_MESSAGE = (
    "A tournament is currently running ({name}). "
    "Closing now will stop it; on restart it will start from scratch and "
    "all recorded games will be discarded.\n\nClose anyway?"
)
_NOT_INSTALLED_MESSAGE = "PyWebView is not installed. Install with: pip install '.[desktop]'"
_HTML_LINE_BREAK = "<br>"

# save_pgn result keys, read by the page's desktop bridge.
_OK_KEY = "ok"
_PATH_KEY = "path"

log = logging.getLogger(__name__)


def _suggested_port(port: int) -> int:
    """The next port, offered when ``port`` can't be used."""
    return port + 1


def _html_lines(text: str) -> str:
    """Escape first, then turn newlines into breaks: text can carry a raw
    exception string, so never interpolate it unescaped into HTML."""
    return html_escape(text).replace("\n", _HTML_LINE_BREAK)


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
            return _save_failed("window not attached")
        try:
            result = window.create_file_dialog(
                self._save_dialog_kind,
                save_filename=default_filename,
                file_types=_PGN_FILE_TYPES,
            )
        except Exception as exc:
            log.error("save_pgn: dialog failed", exc_info=True)
            return _save_failed(str(exc))
        if not result:
            return {_OK_KEY: False, "cancelled": True}
        # PyWebView returns either a string or a sequence depending on
        # backend; normalize.
        path = result[0] if isinstance(result, (list, tuple)) else result
        try:
            Path(path).write_text(pgn_text, encoding="utf-8", newline="")
        except OSError as exc:
            log.error("save_pgn: write failed for %s", path, exc_info=True)
            return _save_failed(str(exc), **{_PATH_KEY: str(path)})
        return {_OK_KEY: True, _PATH_KEY: str(path)}

    def toggle_fullscreen(self) -> None:
        window = self._window
        if window is not None:
            window.toggle_fullscreen()


def _save_failed(error: str, **extra) -> dict:
    return {_OK_KEY: False, ERROR_KEY: error, **extra}


def _port_in_use(host: str, port: int) -> bool:
    """True iff binding (host, port) fails with address-in-use/permission.
    A pre-flight check: uvicorn swallows the bind OSError and sys.exit(1)s,
    so we detect the common case ourselves to show a useful message. errno
    is portable (EADDRINUSE on POSIX and Windows/WSAEADDRINUSE alike).

    SO_REUSEADDR matches uvicorn's listener so a port in TIME_WAIT isn't
    misread as in-use -- but only off Windows, where REUSEADDR instead lets
    a bind steal an actively-listening port (which would misread it free)."""
    bind_host = LOOPBACK_HOST if host == WILDCARD_HOST else host
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not is_windows():
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((bind_host, port))
        return False
    except OSError as exc:
        return exc.errno in (errno.EADDRINUSE, errno.EACCES)
    finally:
        probe.close()


def _apply_window_icon(window) -> None:
    """Windows only: a window's icon comes from the launcher exe (the
    Python logo in dev), so set app.ico explicitly via WM_SETICON."""
    if not is_windows() or not _WINDOW_ICON.is_file():
        return
    try:
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = (
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
        )
        hicon = user32.LoadImageW(
            None, str(_WINDOW_ICON), _IMAGE_ICON, 0, 0,
            _LR_LOADFROMFILE | _LR_DEFAULTSIZE,
        )
        if not hicon:
            log.error("window icon: LoadImageW failed for %s", _WINDOW_ICON)
            return
        hwnd = window.native.Handle.ToInt64()
        for which in (_ICON_SMALL, _ICON_BIG):
            user32.SendMessageW(hwnd, _WM_SETICON, which, hicon)
    except Exception:
        log.error("window icon: failed to apply", exc_info=True)


def show_error(title: str, message: str, details: str | None = None) -> None:
    if webview is None:
        return
    try:
        safe = _html_lines(message)
        details_block = ""
        height = _ERROR_WINDOW_HEIGHT
        if details:
            safe_details = _html_lines(details)
            # Backtick spans become <code> chips (escape first, so the
            # substitution can never introduce markup from the message).
            safe_details = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe_details)
            details_block = (
                "<details><summary>" + _ERROR_DETAILS_SUMMARY + "</summary><p>"
                + safe_details + "</p></details>"
            )
            height = _ERROR_WINDOW_HEIGHT_DETAILS
        html = (
            "<html><head><style>"
            "body{margin:0;display:flex;flex-direction:column;align-items:center;"
            "justify-content:center;height:100vh;font-family:system-ui,sans-serif;"
            "background:#1e1e1e;color:#ccc;}"
            "p{text-align:center;font-size:14px;padding:0 24px;line-height:1.5;}"
            "details{font-size:13px;padding:0 24px;}"
            "summary{cursor:pointer;text-align:center;color:#8ab4f8;}"
            "details p{text-align:left;font-size:13px;color:#aaa;}"
            "code{font-family:ui-monospace,Consolas,monospace;font-size:12px;"
            "color:#f59e0b;background:#2a2a2a;padding:1px 4px;border-radius:3px;}"
            "</style></head><body><p>" + safe + "</p>" + details_block
            + "</body></html>"
        )
        # Passing screen= makes pywebview compute a centered Location itself;
        # the CenterScreen default is applied too late (post handle creation)
        # and the window lands at the OS default cascade position instead.
        window = webview.create_window(
            title, html=html, width=_ERROR_WINDOW_WIDTH, height=height,
            screen=webview.screens[0],
        )
        window.events.shown += _apply_window_icon
        webview.start()
    except Exception:
        log.error("error window: failed to show %r: %s", title, message, exc_info=True)


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


def run_desktop(host: str, port: int, width: int, height: int) -> None:
    if webview is None:
        raise SystemExit(_NOT_INSTALLED_MESSAGE)

    # Pre-flight the port: uvicorn catches the bind OSError and sys.exit(1)s,
    # which surfaces as an unhelpful SystemExit(1). Detect the common case
    # here so the user gets a clear "port in use" window with guidance.
    if _port_in_use(host, port):
        show_error(
            _STARTUP_ERROR_TITLE,
            _PORT_IN_USE_MESSAGE.format(port=port, alt=_suggested_port(port)),
        )
        raise SystemExit(f"port {port} already in use")

    settings = Settings(host=host, port=port)
    # Pin the token so create_app picks up the same value via Settings()
    # (rather than rolling a new random one) -- the /auth handshake below
    # validates against this token.
    os.environ.setdefault(ENV_TOKEN, settings.token)

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
    app.state.lan_listener = LanListener(server, host)
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
            _STARTUP_ERROR_TITLE,
            _STARTUP_FAILED_MESSAGE.format(port=port, alt=_suggested_port(port)),
        )
        raise SystemExit(f"server startup failed: {signal.error!r}")

    window_host = LOOPBACK_HOST if host == WILDCARD_HOST else host
    # Use the cookie handshake: /auth validates the token, sets an HttpOnly
    # cookie, then 303s to /ui/. The window's history never holds the token.
    url = entry_url(settings, window_host)
    api = JsApi(save_dialog_kind=webview.FileDialog.SAVE)
    window = webview.create_window(
        APP_NAME, url, width=width, height=height,
        min_size=(_MIN_WINDOW_WIDTH, _MIN_WINDOW_HEIGHT), js_api=api,
    )
    api.attach(window)
    window.events.shown += _apply_window_icon
    window.events.closing += _make_close_handler(app, window)
    webview.start(private_mode=False, storage_path=str(app_data_dir()))

    server.should_exit = True
    thread.join(timeout=_SERVER_SHUTDOWN_TIMEOUT)
