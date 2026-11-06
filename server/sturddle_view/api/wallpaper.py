"""Studio wallpaper: one global image the user uploads from their machine,
or a bundled theme's.

Stored as a single ``image.<ext>`` under the app data dir; its mtime is the
version the client cache-busts the image URL with. The display style (fade,
fit) lives beside it in ``style.json`` and survives image changes. A theme is
an image under the themes dir plus an optional ``<id>.json`` preset tuned
for it: its name is the menu's label (else the id), its fit/fade set the
style here, the rest is the client's to apply. The applied theme's id
lives in ``theme.txt`` until the image changes.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .. import app_data_dir
from .._atomic import atomic_write_bytes, atomic_write_json, atomic_write_text
from ..auth import require_token
from ..config import WEB_DIR
from ..env_utils import env_int, env_path
from ._http import bad_request, not_found

log = logging.getLogger(__name__)

router = APIRouter(prefix="/wallpaper", tags=["wallpaper"], dependencies=[Depends(require_token)])

MAX_WALLPAPER_BYTES = env_int("SV_MAX_WALLPAPER_BYTES", 20 * 1024 * 1024, min_value=1)
# Fade blends the image into the app background, in percent (100 = gone).
MAX_FADE_PCT = env_int("SV_WALLPAPER_MAX_FADE", 100, min_value=0)
FITS = ("cover", "contain", "stretch", "center", "tile")
_DEFAULT_STYLE = {"fade": 0, "fit": FITS[0]}
_DIR_ENV = "SV_WALLPAPER_DIR"
_DIRNAME = "wallpaper"
_STEM = "image"
_STYLE_FILENAME = "style.json"
_EXT_BY_TYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_TYPE_BY_EXT = {ext: ctype for ctype, ext in _EXT_BY_TYPE.items()}
_VERSION_KEY = "version"
_FADE_KEY = "fade"
_FIT_KEY = "fit"
_FADE_MAX_KEY = "fade_max"
_FITS_KEY = "fits"
_CONTENT_TYPE_HEADER = "content-type"
_CONTENT_LENGTH_HEADER = "content-length"
_NO_WALLPAPER = "no wallpaper"
_THEMES_ENV = "SV_THEMES_DIR"
_THEMES_DIRNAME = "themes"
_PRESET_EXT = ".json"
_THEMES_KEY = "themes"
_PRESET_KEY = "preset"
# A theme's id (file stem) and display name (its preset's, else the id).
_ID_KEY = "id"
_NAME_KEY = "name"
# The applied theme's id, beside the image; any other image change drops it.
_THEME_FILENAME = "theme.txt"
_THEME_KEY = "theme"


def _root() -> Path:
    return env_path(_DIR_ENV, app_data_dir() / _DIRNAME)


def _themes() -> dict[str, tuple[Path, dict, str]]:
    """Bundled themes by id (file stem), sorted: image, preset (empty when
    none) and display name (the preset's, else the id)."""
    root = env_path(_THEMES_ENV, WEB_DIR / _THEMES_DIRNAME)
    if not root.is_dir():
        return {}
    themes = {}
    for p in sorted(root.iterdir()):
        if p.suffix.lower() not in _TYPE_BY_EXT or not p.is_file():
            continue
        preset = _read_json(p.with_suffix(_PRESET_EXT), "theme preset")
        preset = preset if isinstance(preset, dict) else {}
        name = preset.get(_NAME_KEY)
        themes[p.stem] = (p, preset, name if isinstance(name, str) and name.strip() else p.stem)
    return themes


def _read_json(path: Path, what: str):
    """The file's JSON, or None when missing or unreadable (logged)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("ignoring unreadable %s %s", what, path, exc_info=True)
        return None


def _valid_style(fields: dict) -> dict:
    """The fields that are valid style keys and values."""
    return {
        k: fields[k] for k, check in _STYLE_CHECKS.items()
        if k in fields and check(fields[k]) is None
    }


def _stored() -> Path | None:
    """The stored image, or None when no wallpaper is set."""
    for ext in _TYPE_BY_EXT:
        path = _root() / f"{_STEM}{ext}"
        if path.is_file():
            return path
    return None


def _remove_all(keep: str | None = None) -> None:
    """Delete the stored image(s), sparing the ``keep`` extension, and the
    applied theme's id."""
    for ext in _TYPE_BY_EXT:
        if ext != keep:
            (_root() / f"{_STEM}{ext}").unlink(missing_ok=True)
    (_root() / _THEME_FILENAME).unlink(missing_ok=True)


def _current_theme(themes: dict[str, tuple[Path, dict, str]]) -> str | None:
    """The applied theme's id, or None when none is or it is gone."""
    try:
        theme_id = (_root() / _THEME_FILENAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("ignoring unreadable wallpaper theme id", exc_info=True)
        return None
    return theme_id if theme_id in themes else None


def _store(data: bytes, ext: str) -> None:
    """Replace the stored image with data, an ``ext`` image."""
    atomic_write_bytes(_root() / f"{_STEM}{ext}", data)
    _remove_all(keep=ext)


def _fade_error(v) -> str | None:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_FADE_PCT:
        return f"{_FADE_KEY} must be an integer 0..{MAX_FADE_PCT}"
    return None


def _fit_error(v) -> str | None:
    return None if v in FITS else f"{_FIT_KEY} must be one of {', '.join(FITS)}"


_STYLE_CHECKS = {_FADE_KEY: _fade_error, _FIT_KEY: _fit_error}


def _style() -> dict:
    """Stored style over the defaults; a missing/bad file or field reads as default."""
    stored = _read_json(_root() / _STYLE_FILENAME, "wallpaper style")
    return {**_DEFAULT_STYLE, **(_valid_style(stored) if isinstance(stored, dict) else {})}


def _info() -> dict:
    path = _stored()
    themes = _themes()
    return {
        _VERSION_KEY: path.stat().st_mtime_ns if path else None,
        **_style(),
        _FADE_MAX_KEY: MAX_FADE_PCT,
        _FITS_KEY: list(FITS),
        _THEMES_KEY: [{_ID_KEY: tid, _NAME_KEY: name} for tid, (_image, _preset, name) in themes.items()],
        _THEME_KEY: _current_theme(themes) if path else None,
    }


def _too_large(size: int) -> HTTPException:
    log.warning("rejected oversized wallpaper: %d+ bytes (cap %d)", size, MAX_WALLPAPER_BYTES)
    return bad_request(f"image too large (max {MAX_WALLPAPER_BYTES} bytes)")


async def _read_capped(request: Request) -> bytes:
    """The request body, rejected once it passes MAX_WALLPAPER_BYTES: a
    declared length up front, else while streaming, so an oversized upload
    is never buffered whole."""
    declared = request.headers.get(_CONTENT_LENGTH_HEADER, "")
    if declared.isdigit() and int(declared) > MAX_WALLPAPER_BYTES:
        raise _too_large(int(declared))
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_WALLPAPER_BYTES:
            raise _too_large(len(buf))
    return bytes(buf)


@router.get("/info")
def wallpaper_info() -> dict:
    """Image ``version`` (null when none is set), the style, and its bounds."""
    return _info()


@router.get("")
def get_wallpaper() -> FileResponse:
    path = _stored()
    if path is None:
        raise not_found(_NO_WALLPAPER)
    return FileResponse(path, media_type=_TYPE_BY_EXT[path.suffix])


@router.put("")
async def put_wallpaper(request: Request) -> dict:
    """Replace the wallpaper with the raw image body (PNG, JPEG or WebP)."""
    ctype = request.headers.get(_CONTENT_TYPE_HEADER, "").split(";")[0].strip().lower()
    ext = _EXT_BY_TYPE.get(ctype)
    if ext is None:
        raise bad_request(f"unsupported image type {ctype!r}; use PNG, JPEG or WebP")
    data = await _read_capped(request)
    if not data:
        raise bad_request("empty image")
    _store(data, ext)
    return _info()


@router.put("/theme/{theme_id}")
def put_theme(theme_id: str) -> dict:
    """Set a theme: its image becomes the wallpaper and its preset's fit/fade
    the style; returns the info plus the whole preset (empty when none)."""
    theme = _themes().get(theme_id)
    if theme is None:
        raise not_found(f"no theme {theme_id!r}")
    image, preset, _name = theme
    _store(image.read_bytes(), image.suffix.lower())
    atomic_write_text(_root() / _THEME_FILENAME, theme_id)
    style = _valid_style(preset)
    if style:
        atomic_write_json(_root() / _STYLE_FILENAME, {**_style(), **style})
    return {**_info(), _PRESET_KEY: preset}


@router.delete("")
def delete_wallpaper() -> dict:
    _remove_all()
    return _info()


@router.put("/style")
def put_style(payload: dict) -> dict:
    """Merge ``fade`` and/or ``fit`` into the stored style."""
    unknown = set(payload) - set(_STYLE_CHECKS)
    if unknown:
        raise bad_request(f"unknown style keys: {', '.join(sorted(unknown))}")
    for key, value in payload.items():
        err = _STYLE_CHECKS[key](value)
        if err:
            raise bad_request(err)
    atomic_write_json(_root() / _STYLE_FILENAME, {**_style(), **payload})
    return _info()
