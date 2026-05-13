"""PyInstaller entry point for the standalone desktop exe.

Forces --desktop so the exe always opens in the native PyWebView window.
Not part of the installed package -- used only by the build script.
"""
import sys

from sturddle_view.__main__ import main

if "--desktop" not in sys.argv:
    sys.argv.append("--desktop")

main()
