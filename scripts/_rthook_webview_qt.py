"""PyInstaller runtime hook, Linux --linux-webview qt: point pywebview at the
bundled Qt backend (it tries GTK first otherwise)."""
import os

os.environ.setdefault("PYWEBVIEW_GUI", "qt")
