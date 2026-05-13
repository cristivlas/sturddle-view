"""PyWebView wrapper. Runs uvicorn in a background thread, then opens a native window."""
from __future__ import annotations

import threading
import time

import uvicorn
from platformdirs import user_data_dir

from . import APP_NAME
from .config import Settings


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

    config = uvicorn.Config(
        "sturddle_view.app:create_app",
        host=host,
        port=port,
        factory=True,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait briefly for the server to come up before opening the window.
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)

    window_host = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{window_host}:{port}/?token={settings.token}"
    webview.create_window("sturddle-view", url, width=width, height=height)
    webview.start(private_mode=False, storage_path=user_data_dir(APP_NAME, appauthor=False))

    server.should_exit = True
    thread.join(timeout=5)
