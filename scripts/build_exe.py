#!/usr/bin/env python3
"""Build a self-contained standalone desktop exe via PyInstaller.

Steps:
  1. Read version from server/sturddle_view/__init__.py.
  2. Delete and recreate build_venv/ with a stock pip.
  3. Install .[desktop] + pyinstaller (Linux --linux-webview qt: + PyQt6).
  4. Write a PyInstaller --onefile --windowed spec with full data + hidden
     imports + hooks config, and build it.
  5. Write dist/<name>.sha256 next to the exe.

Linux web view backend, --linux-webview:
  gtk     (default) GTK bundled; PyGObject comes from the distro (python3-gi),
          so the build venv sees system site-packages. Needs WebKitGTK at
          run time.
  qt      PyQt6 + QtWebEngine from pip, all bundled. Largest, self-contained.
  system  No GUI toolkit bundled; at run time the exe uses the distro's
          PyGObject (for the same Python version) and WebKitGTK.

Run from the repository root:
    python scripts/build_exe.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = ROOT / "scripts" / "_exe_entry.py"
PLAYBOOK_DATA = ROOT / "server" / "sturddle_view" / "play" / "playbook_data"
VENV_DIR = ROOT / "build_venv"
BUILDS_DIR = ROOT / "dist"
IS_LINUX = sys.platform.startswith("linux")
LINUX_WEBVIEWS = ("gtk", "qt", "system")
# The backend a venv was built for: --reuse-venv must not mix them.
_WEBVIEW_MARKER = VENV_DIR / ".linux-webview"
_RUNTIME_HOOKS = {
    "qt": ROOT / "scripts" / "_rthook_webview_qt.py",
    "system": ROOT / "scripts" / "_rthook_webview_system.py",
}

# uvicorn resolves protocol/loop implementations at runtime via importlib.
# anyio selects its async backend at runtime. webview selects its platform
# backend at runtime. All must be present in the bundle.
_HIDDEN_IMPORTS: list[str] = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
    # uvicorn loads the app by string reference at runtime
    "sturddle_view.app",
    "webview",
    "webview.platforms",
    "webview.platforms.winforms",
]

# Packages whose data files / non-Python resources must be collected.
# keyring: backends load via entry-points at runtime, so static analysis
# misses them -- without collect-all a frozen build falls back to the fail
# backend and silently can't store API keys.
_COLLECT_ALL: list[str] = [
    "webview",
    "pydantic",
    "pydantic_settings",
    "keyring",
]


# --linux-webview system: the distro's PyGObject is invisible to PyInstaller,
# so the stdlib modules it imports must be bundled by name.
_SYSTEM_GI_STDLIB: list[str] = [
    "collections", "contextlib", "fcntl", "functools", "importlib", "inspect", "optparse",
    "pkgutil", "re", "signal", "socket", "threading", "types", "warnings",
]

# Unused by the app, but importable from the Linux build venv's system
# site-packages, so optional imports in dependencies would bundle it.
_EXCLUDE_MODULES: list[str] = ["numpy"]

# GTK's hooks otherwise copy every icon theme, GTK theme and translation
# installed on the build machine (~1 GB). Adwaita + hicolor are GTK's icon
# fallbacks; its default theme is built in. The app is English-only.
_HOOKS_CONFIG = {"gi": {"icons": ["Adwaita", "hicolor"], "themes": [], "languages": []}}
# makespec leaves this slot empty in Analysis(...); _write_spec fills it.
_SPEC_HOOKS_SLOT = "hooksconfig={},"


def _read_version() -> str:
    init = ROOT / "server" / "sturddle_view" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    raise RuntimeError(f"__version__ not found in {init}")


def _venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


# gtk: the venv sees system site-packages for the distro's PyGObject (gi);
# pip then installs over them so the bundle doesn't pick up stale distro
# packages.
def _make_venv(system_site: bool) -> Path:
    if VENV_DIR.exists():
        print(f"Removing {VENV_DIR} ...")
        shutil.rmtree(VENV_DIR)
    print(f"Creating venv at {VENV_DIR} (may take a minute) ...")
    sys.stdout.flush()
    subprocess.run(
        [sys.executable, "-m", "venv", *(["--system-site-packages"] if system_site else []), str(VENV_DIR)],
        check=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    python = _venv_python()
    if not python.exists():
        raise RuntimeError(f"venv python not found at {python}")
    print("Venv ready.")
    return python


def _pip(python: Path, system_site: bool, *args: str) -> None:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    subprocess.run(
        [str(python), "-m", "pip", "install", *(["--ignore-installed"] if system_site else []), *args],
        check=True,
        env=env,
    )



def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _makespec_cmd(python: Path, exe_name: str, icon: Path, console: bool, webview: str | None) -> list[str]:
    sep = os.pathsep  # ; on Windows, : on POSIX
    cmd: list[str] = [
        str(python), "-m", "PyInstaller.utils.cliutils.makespec",
        "--onefile",
        "--console" if console else "--windowed",
        f"--icon={icon}",
        f"--name={exe_name}",
        f"--specpath={VENV_DIR}",
        # Bundle the entire web tree (static assets + openings TSVs).
        f"--add-data={ROOT / 'web'}{sep}web",
        # Shipped playbook text, beside the module that reads it.
        f"--add-data={PLAYBOOK_DATA}{sep}{PLAYBOOK_DATA.relative_to(ROOT / 'server')}",
    ]
    for imp in _HIDDEN_IMPORTS + (_SYSTEM_GI_STDLIB if webview == "system" else []):
        cmd.append(f"--hidden-import={imp}")
    for pkg in _COLLECT_ALL:
        cmd.append(f"--collect-all={pkg}")
    for mod in _EXCLUDE_MODULES:
        cmd.append(f"--exclude-module={mod}")
    if webview in _RUNTIME_HOOKS:
        cmd.append(f"--runtime-hook={_RUNTIME_HOOKS[webview]}")
    cmd.append(str(ENTRY_POINT))
    return cmd


# The command line can't pass hooks config, so it goes into the spec.
def _write_spec(python: Path, exe_name: str, icon: Path, console: bool, webview: str | None) -> Path:
    subprocess.run(_makespec_cmd(python, exe_name, icon, console, webview), check=True)
    spec = VENV_DIR / f"{exe_name}.spec"
    text = spec.read_text(encoding="utf-8")
    if text.count(_SPEC_HOOKS_SLOT) != 1:
        raise RuntimeError(f"{spec}: expected one '{_SPEC_HOOKS_SLOT}' to fill")
    spec.write_text(text.replace(_SPEC_HOOKS_SLOT, f"hooksconfig={_HOOKS_CONFIG!r},"), encoding="utf-8")
    return spec


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Build standalone desktop exe")
    parser.add_argument(
        "--reuse-venv", action="store_true",
        help="Skip venv creation and pip installs if build_venv already exists",
    )
    parser.add_argument(
        "--console", action="store_true",
        help="Keep console window (for debugging; omit for release)",
    )
    parser.add_argument(
        "--linux-webview", choices=LINUX_WEBVIEWS, default=LINUX_WEBVIEWS[0],
        help="Linux web view backend to bundle (see module docstring); ignored elsewhere",
    )
    args = parser.parse_args()
    webview = args.linux_webview if IS_LINUX else None
    system_site = webview == "gtk"

    version = _read_version()
    exe_name = f"sturddle-view-{version}"
    print(f"Building {exe_name} ...")

    python = _venv_python()
    # No marker: a venv from before --linux-webview, which was always gtk.
    built_for = _WEBVIEW_MARKER.read_text(encoding="utf-8") if _WEBVIEW_MARKER.exists() else LINUX_WEBVIEWS[0]
    if args.reuse_venv and VENV_DIR.exists() and IS_LINUX and built_for != webview:
        print(f"{VENV_DIR} was built for --linux-webview {built_for}, not {webview}: recreating it")
        args.reuse_venv = False
    if args.reuse_venv and VENV_DIR.exists():
        print(f"Reusing existing venv at {VENV_DIR}")
        # Always reinstall the local package so code changes are picked up.
        # --no-build-isolation: reuse already-installed build deps (setuptools/wheel).
        print("--- reinstalling local package ---")
        _pip(python, system_site, "--no-build-isolation", f"{ROOT}[desktop]")
    else:
        python = _make_venv(system_site)
        print("--- upgrading pip ---")
        _pip(python, system_site, "--upgrade", "pip")

        print("--- installing build deps ---")
        _pip(python, system_site, "setuptools", "wheel")

        print("--- installing package + desktop deps ---")
        _pip(python, system_site, "--no-build-isolation", f"{ROOT}[desktop]")
        if webview == "qt":
            print("--- installing Qt web view ---")
            _pip(python, system_site, "pywebview[qt]")

        print("--- installing pyinstaller ---")
        _pip(python, system_site, "pyinstaller")
        if webview:
            _WEBVIEW_MARKER.write_text(webview, encoding="utf-8")

    icon = ROOT / "web" / "app.ico"
    if not icon.exists():
        raise FileNotFoundError(f"Icon not found: {icon}")
    BUILDS_DIR.mkdir(exist_ok=True)

    print("--- running pyinstaller ---")
    spec = _write_spec(python, exe_name, icon, args.console, webview)
    subprocess.run([
        str(python), "-m", "PyInstaller", "--noconfirm",
        f"--distpath={BUILDS_DIR}", f"--workpath={VENV_DIR / 'pyinstaller_work'}", str(spec),
    ], check=True)

    suffix = ".exe" if sys.platform == "win32" else ""
    exe_path = BUILDS_DIR / f"{exe_name}{suffix}"
    if not exe_path.exists():
        raise FileNotFoundError(f"Expected output not found: {exe_path}")

    digest = _sha256(exe_path)
    # sha256sum-compatible format: "<digest>  <filename>"
    sha_path = BUILDS_DIR / f"{exe_name}.sha256"
    sha_path.write_text(f"{digest}  {exe_path.name}\n", encoding="utf-8")

    print(f"Exe:     {exe_path}")
    print(f"SHA-256: {digest}")
    print(f"Written: {sha_path}")


if __name__ == "__main__":
    main()
