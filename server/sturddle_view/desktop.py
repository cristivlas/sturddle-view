"""PyWebView wrapper. Runs uvicorn in a background thread, then opens a native window."""
from __future__ import annotations

import os
import threading

import uvicorn
from platformdirs import user_data_dir

from . import APP_NAME
from ._uvicorn_signal import make_signalling_server
from .config import Settings

_SERVER_STARTUP_TIMEOUT = 5.0
_SERVER_SHUTDOWN_TIMEOUT = 5.0


def show_error(title: str, message: str) -> None:
    try:
        import webview  # type: ignore[import-untyped]
        html = (
            "<html><head><style>"
            "body{margin:0;display:flex;align-items:center;justify-content:center;"
            "height:100vh;font-family:system-ui,sans-serif;background:#1e1e1e;color:#ccc;}"
            "p{text-align:center;font-size:14px;padding:0 24px;}"
            "</style></head><body><p>" + message + "</p></body></html>"
        )
        w = webview.create_window(title, html=html, width=400, height=150)
        webview.start()
    except Exception:
        pass


def run_desktop(host: str, port: int, width: int = 1280, height: int = 800) -> None:
    try:
        import webview  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit("PyWebView is not installed. Install with: pip install '.[desktop]'") from exc

    settings = Settings(host=host, port=port)
    # Pin the token so the uvicorn-spawned create_app() picks up the same
    # value via Settings() (rather than rolling a new random one).
    os.environ.setdefault("SV_TOKEN", settings.token)

    config = uvicorn.Config(
        "sturddle_view.app:create_app",
        host=host,
        port=port,
        factory=True,
        log_config=None,
        access_log=False,
    )

    server, started = make_signalling_server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not started.wait(timeout=_SERVER_STARTUP_TIMEOUT):
        server.should_exit = True
        thread.join(timeout=_SERVER_SHUTDOWN_TIMEOUT)
        raise SystemExit("server did not start within timeout")

    window_host = "127.0.0.1" if host == "0.0.0.0" else host
    # Use the cookie handshake: /auth validates the token, sets an HttpOnly
    # cookie, then 303s to /ui/. The window's history never holds the token.
    url = f"http://{window_host}:{port}/auth?token={settings.token}"
    webview.create_window("sturddle-view", url, width=width, height=height)
    webview.start(private_mode=False, storage_path=user_data_dir(APP_NAME, appauthor=False))

    server.should_exit = True
    thread.join(timeout=_SERVER_SHUTDOWN_TIMEOUT)
