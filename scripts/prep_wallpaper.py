#!/usr/bin/env python3
"""Prepare a theme for the Wallpaper menu's Themes from a source image.

Writes ``web/themes/<name>.webp`` (resized to fit --max-edge, never upscaled,
metadata stripped) and a starter ``<name>.json`` preset guessed from the
image: its brightness sets the wallpaper fade and frost brightness, its
busyness the frost blur. Hand-tune the preset after; an existing one is kept
unless --force.

Usage:
  python scripts/prep_wallpaper.py photo.jpg
  python scripts/prep_wallpaper.py photo.jpg --name dusk --force
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

REPO = Path(__file__).resolve().parent.parent
THEMES_DIR = REPO / "web" / "themes"
IMAGE_EXT = ".webp"
PRESET_EXT = ".json"
MAX_EDGE = 1920
# Below this long edge, cover's stretch turns soft: warn.
SOFT_EDGE = 1280
# Cover on a 16:9 screen crops what doesn't fit: warn past this share lost.
SCREEN_ASPECT = 16 / 9
MAX_CROP = 0.25
QUALITY = 80
# Busyness is measured on a copy this size (edge density is size-dependent).
PROBE_EDGE = 512
LUM_MAX = 255
PCT = 100
PCT_STEP = 5
# Within web/app/frost.js SLIDERS; brightness never brightens (a dark image
# brightened under light text loses contrast); fade stays shy of fully gone.
BLUR_RANGE = (0, 20)
BRIGHTNESS_RANGE = (20, 100)
FADE_RANGE = (0, 80)
# Frost dims the image behind a panel toward this luminance (0..1).
TARGET_LUM = 0.25
# Image luminance past which the wallpaper starts fading into the background.
FADE_LUM_FLOOR = 0.3
# Blur px per unit of mean edge strength (0..1).
BLUR_PER_BUSY = 80
FIT = "cover"
# Panel names as web/app/theme.js maps them. Their fade (0 = opaque) drops
# from PANEL_FADE_CALM by PANEL_FADE_PER_BUSY per unit of busyness, so text
# over a busy image stays readable.
PANELS = ("ai", "evalbar", "ucilog", "pvtable", "studioLeft", "studioRight")
PANEL_FADE_CALM = 60
PANEL_FADE_PER_BUSY = 300
PANEL_FADE_RANGE = (0, 60)


def clamp(v: float, bounds: tuple[int, int]) -> int:
    lo, hi = bounds
    return int(min(max(v, lo), hi))


def round_step(v: float) -> int:
    return int(round(v / PCT_STEP) * PCT_STEP)


def slug(stem: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def measure(img: Image.Image) -> tuple[float, float]:
    """Mean luminance and mean edge strength, both 0..1."""
    gray = img.convert("L")
    lum = ImageStat.Stat(gray).mean[0] / LUM_MAX
    probe = gray.copy()
    probe.thumbnail((PROBE_EDGE, PROBE_EDGE))
    busy = ImageStat.Stat(probe.filter(ImageFilter.FIND_EDGES)).mean[0] / LUM_MAX
    return lum, busy


def guess_preset(lum: float, busy: float) -> dict:
    brightness = PCT * TARGET_LUM / lum if lum else BRIGHTNESS_RANGE[1]
    panel_fade = clamp(round_step(PANEL_FADE_CALM - busy * PANEL_FADE_PER_BUSY), PANEL_FADE_RANGE)
    return {
        "fit": FIT,
        "fade": clamp(round_step(max(0.0, lum - FADE_LUM_FLOOR) * PCT), FADE_RANGE),
        "frost": {
            "blur": clamp(round(busy * BLUR_PER_BUSY), BLUR_RANGE),
            "brightness": clamp(round_step(brightness), BRIGHTNESS_RANGE),
        },
        "panels": {name: panel_fade for name in PANELS},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path)
    ap.add_argument("--name", help="theme name (default: the source's file name)")
    ap.add_argument("--max-edge", type=int, default=MAX_EDGE)
    ap.add_argument("--quality", type=int, default=QUALITY)
    ap.add_argument("--force", action="store_true", help="overwrite an existing preset")
    args = ap.parse_args()

    name = slug(args.name or args.source.stem)
    if not name:
        ap.error("theme name is empty")
    with Image.open(args.source) as src:
        # Honor camera rotation before the EXIF goes.
        img = ImageOps.exif_transpose(src).convert("RGB")
    img.thumbnail((args.max_edge, args.max_edge), Image.Resampling.LANCZOS)
    if max(img.size) < SOFT_EDGE:
        print(f"warning: {img.width}x{img.height} is under {SOFT_EDGE}px; it will look soft stretched")
    aspect = img.width / img.height
    crop = 1 - min(aspect / SCREEN_ASPECT, SCREEN_ASPECT / aspect)
    if crop > MAX_CROP:
        print(f"warning: aspect {aspect:.2f} vs 16:9 screen; cover crops {crop:.0%} of it")
    THEMES_DIR.mkdir(parents=True, exist_ok=True)
    out = THEMES_DIR / f"{name}{IMAGE_EXT}"
    img.save(out, quality=args.quality, method=6)
    print(f"{out.relative_to(REPO)}: {img.width}x{img.height}, {out.stat().st_size // 1024} KiB")

    preset_path = out.with_suffix(PRESET_EXT)
    if preset_path.exists() and not args.force:
        print(f"{preset_path.relative_to(REPO)}: kept (--force to overwrite)")
        return
    lum, busy = measure(img)
    preset = guess_preset(lum, busy)
    preset_path.write_text(json.dumps(preset, indent=2) + "\n", encoding="utf-8")
    print(f"{preset_path.relative_to(REPO)}: luminance {lum:.2f}, busyness {busy:.3f}")


if __name__ == "__main__":
    main()
