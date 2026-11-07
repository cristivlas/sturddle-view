"""PyInstaller runtime hook, Linux --linux-webview system: the bundle has no
GUI toolkit, so pywebview's GTK backend imports the distro's PyGObject
(python3-gi), which loads the distro's WebKitGTK. Appended, so the bundle's
own modules still win."""
import sys

_PY = f"python3.{sys.version_info.minor}"
sys.path += ["/usr/lib/python3/dist-packages", f"/usr/lib/{_PY}/site-packages", f"/usr/lib64/{_PY}/site-packages"]
