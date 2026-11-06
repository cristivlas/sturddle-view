"""PyInstaller entry point for the standalone desktop exe.

Plain dispatch to the package CLI. Frozen-specific behavior (--desktop
default, "proxy" subcommand) lives in sturddle_view.__main__ / _runtime.
Not part of the installed package -- used only by the build script.
"""
from sturddle_view.__main__ import main

main()
