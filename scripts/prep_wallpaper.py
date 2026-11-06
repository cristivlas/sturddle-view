#!/usr/bin/env python3
"""Prepare a theme for the Appearance menu's Theme list from a source image.

Writes ``web/themes/<name>.webp`` (resized to fit --max-edge, never upscaled,
metadata stripped) and a ``<name>.json`` preset tuned for it:

- Readability is a hard rule. The app's compositing is simulated on a 16:9
  screen: wallpaper fade, then frost (blur, brightness) behind frosted panels,
  or the panel's own background at its transparency behind faded ones. Over
  all but the brightest few percent of what lies behind text (frost, being
  blurred, may keep more stray specks), each text color keeps the lesser of
  its WCAG target and the contrast it has with no wallpaper.
- Within that, the most wallpaper shows: least fade, brightest and least
  blurred frost, most panel transparency (*_WEIGHT, BLUR_COST).
- By-eye offsets then apply: panels less see-through (PANELS), frost
  brighter (FROST_BRIGHTNESS_OFFSET), which can take frost past the rule.
- The board style is the one whose dark squares best match the image's
  dominant hue; a muted image gets the most neutral board.

The preset's ``name`` (the menu label, the file stem at first) is edited by
hand; --force keeps it. An existing preset is kept unless --force.

Usage:
  python scripts/prep_wallpaper.py photo.jpg
  python scripts/prep_wallpaper.py photo.jpg --name dusk --force
"""
from __future__ import annotations

import argparse
import colorsys
import json
import math
import re
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageEnhance, ImageFilter, ImageOps

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "web"
THEMES_DIR = WEB / "themes"
STYLES_CSS = WEB / "styles.css"
BOARD_STYLES_JS = WEB / "app" / "board-styles.js"
BOARD_CSS = WEB / "vendor" / "cm-chessboard" / "assets" / "chessboard.css"
IMAGE_EXT = ".webp"
PRESET_EXT = ".json"
MAX_EDGE = 1920
# Below this long edge, cover's stretch turns soft: warn.
SOFT_EDGE = 1280
# Cover on a 16:9 screen crops what doesn't fit: warn past this share lost.
SCREEN_ASPECT = 16 / 9
MAX_CROP = 0.25
QUALITY = 80
# WebP encoder effort (0..6): past this, size gains are slight and slow.
WEBP_METHOD = 4
FIT = "cover"
# Preset key for the theme's display name.
NAME_KEY = "name"

# The simulated screen: SCREEN_W CSS px wide, rendered at SIM_SCALE for speed.
SCREEN_W = 1920
SIM_W, SIM_H = 320, 180
SIM_SCALE = SIM_W / SCREEN_W
PCT = 100
PCT_STEP = 5
# Knob ranges: frost within web/app/frost.js SLIDERS, never brightening; fade
# shy of fully gone; panels never past see-through enough to stay panels. The
# floors are taste: frost no darker, panels no more solid; the fade makes up.
FADE_RANGE = (0, 80)
BLUR_RANGE = (2, 20)
BRIGHTNESS_RANGE = (40, 100)
PANEL_FADE_RANGE = (10, 60)
# Score: wallpaper shown (1 - fade) times frost and panel see-through; a px of
# blur costs BLUR_COST of the frost's share at full blur.
FROST_WEIGHT = 0.5
PANEL_WEIGHT = 0.5
BLUR_COST = 0.5

# Readability: WCAG targets per text color token, checked on a percentile of
# the backdrop behind text: frost blurs specks out, a faded panel doesn't.
TEXT_TARGETS = {"--fg": 4.5}
FROST_PERCENTILE = 0.95
PANEL_PERCENTILE = 0.97
# Surfaces behind text with no wallpaper: frosted panels show the view
# background, faded windows their panel fill.
FROST_SURFACE = "--bg"
PANEL_SURFACE = "--panel"
WCAG_OFFSET = 0.05
# sRGB transfer curve (IEC 61966-2-1).
SRGB_CUTOFF = 0.04045
SRGB_TOE_SLOPE = 12.92
SRGB_OFFSET = 0.055
SRGB_GAMMA = 2.4
# Backdrop luminance is read off an 8-bit image: Y times Y_SCALE per level
# (clipped), fine enough near the limits, which sit well under 0.255.
Y_SCALE = 1000
LEVELS = 256
SRGB_COEF = (0.2126, 0.7152, 0.0722)

# Board pick: an image or a board's dark square counts as colored from here
# (mean saturation x value, and saturation).
COLORFUL_IMAGE = 0.12
COLORED_BOARD = 0.15
HUE_PROBE = (64, 36)
# App board classes' (light, dark) square colors in styles.css.
BOARD_SQUARE_VARS = ("--sb-light", "--sb-dark")

# Panel names as web/app/theme.js maps them, with an offset (pct points) on
# the tuned transparency, by eye: Play's windows and Studio's tab groups.
PANELS = {"ai": -25, "evalbar": -25, "ucilog": -25, "pvtable": -25, "studioLeft": -30, "studioRight": -30}
# Frost brightness offset (pct points) on the tuned value, by eye.
FROST_BRIGHTNESS_OFFSET = 5
# Board transparency (pct): opaque; readability over the board is not modeled.
BOARD_FADE = 0


def slug(stem: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def to_linear(c: float) -> float:
    if c <= SRGB_CUTOFF:
        return c / SRGB_TOE_SLOPE
    return ((c + SRGB_OFFSET) / (1 + SRGB_OFFSET)) ** SRGB_GAMMA


def luminance(rgb: tuple[int, int, int]) -> float:
    return sum(k * to_linear(v / (LEVELS - 1)) for k, v in zip(SRGB_COEF, rgb))


def contrast(y1: float, y2: float) -> float:
    return (max(y1, y2) + WCAG_OFFSET) / (min(y1, y2) + WCAG_OFFSET)


# Per channel: sRGB byte -> its share of luminance, in Y_SCALE levels.
Y_LUTS = [
    [min(LEVELS - 1, round(k * to_linear(v / (LEVELS - 1)) * Y_SCALE)) for v in range(LEVELS)]
    for k in SRGB_COEF
]


def y_percentile(img: Image.Image, q: float) -> float:
    r, g, b = (ch.point(lut) for ch, lut in zip(img.split(), Y_LUTS))
    hist = ImageChops.add(ImageChops.add(r, g), b).histogram()
    target, seen = q * sum(hist), 0
    for level, n in enumerate(hist):
        seen += n
        if seen >= target:
            return level / Y_SCALE
    return (LEVELS - 1) / Y_SCALE


def must_find(pattern: str, text: str, what: str, flags: int = 0) -> str:
    """Group 1 of pattern in text; exits naming what when the source changed shape."""
    m = re.search(pattern, text, flags)
    if not m:
        raise SystemExit(f"error: {what} not found; update {Path(__file__).name}")
    return m.group(1).strip()


def css_tokens() -> dict[str, tuple[int, int, int]]:
    """The :root color tokens the readability rule reads."""
    root = must_find(r":root\s*\{(.*?)\}", STYLES_CSS.read_text(encoding="utf-8"), ":root block", re.S)
    names = {*TEXT_TARGETS, FROST_SURFACE, PANEL_SURFACE}
    return {n: ImageColor.getrgb(must_find(rf"{n}:\s*([^;]+);", root, n)) for n in names}


def max_backdrop_y(tokens: dict, surface: str) -> float:
    """Brightest backdrop luminance every text color stays readable over:
    each keeps min(its target, its contrast on the bare surface)."""
    base = luminance(tokens[surface])
    limits = []
    for name, target in TEXT_TARGETS.items():
        text = luminance(tokens[name])
        need = min(target, contrast(text, base))
        limits.append((text + WCAG_OFFSET) / need - WCAG_OFFSET)
    return min(limits)


def board_styles() -> dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]]:
    """Board style id -> (light, dark) square colors, one id per color class
    (the first listed), from board-styles.js and the board CSS."""
    js = BOARD_STYLES_JS.read_text(encoding="utf-8")
    vendor = BOARD_CSS.read_text(encoding="utf-8")
    app = STYLES_CSS.read_text(encoding="utf-8")
    styles, seen = {}, set()
    for sid, classes in re.findall(r'"([\w-]+)":\s*\{\s*label:\s*"[^"]*",\s*cssClass:\s*"([^"]+)"', js):
        cls = classes.split()[-1]
        if cls in seen:
            continue
        seen.add(cls)
        light = re.search(rf"\.cm-chessboard\.{cls} \.board \.square\.white \{{\s*fill: ([^;]+);", vendor)
        dark = re.search(rf"\.cm-chessboard\.{cls} \.board \.square\.black \{{\s*fill: ([^;]+);", vendor)
        if light and dark:
            colors = (light.group(1).strip(), dark.group(1).strip())
        else:
            block = must_find(rf"\.cm-chessboard\.{cls} \{{(.*?)\}}", app, f"board class {cls}", re.S)
            colors = tuple(must_find(rf"{v}:\s*([^;]+);", block, f"{cls} {v}") for v in BOARD_SQUARE_VARS)
        styles[sid] = tuple(ImageColor.getrgb(c) for c in colors)
    return styles


def hsv(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    return colorsys.rgb_to_hsv(*(v / (LEVELS - 1) for v in rgb))


def dominant_hue(img: Image.Image) -> tuple[float, float]:
    """Saturation-and-value weighted mean hue (0..1) and mean colorfulness."""
    data = img.resize(HUE_PROBE).tobytes()
    pixels = [tuple(data[i:i + 3]) for i in range(0, len(data), 3)]
    x = y = total = 0.0
    for rgb in pixels:
        h, s, v = hsv(rgb)
        w = s * v
        x += w * math.cos(2 * math.pi * h)
        y += w * math.sin(2 * math.pi * h)
        total += w
    return (math.atan2(y, x) / (2 * math.pi)) % 1, total / len(pixels)


def hue_gap(a: float, b: float) -> float:
    d = abs(a - b) % 1
    return min(d, 1 - d)


def pick_board(img: Image.Image) -> tuple[str, float, float]:
    hue, colorful = dominant_hue(img)
    styles = {sid: hsv(dark) for sid, (_light, dark) in board_styles().items()}
    colored = {sid: hs for sid, hs in styles.items() if hs[1] >= COLORED_BOARD}
    if colorful >= COLORFUL_IMAGE and colored:
        sid = min(colored, key=lambda s: hue_gap(colored[s][0], hue))
    else:
        sid = min(styles, key=lambda s: styles[s][1])
    return sid, hue, colorful


def pct_steps(bounds: tuple[int, int], step: int = PCT_STEP) -> range:
    return range(bounds[0], bounds[1] + 1, step)


def highest_passing(steps: range, ok) -> int | None:
    """The highest step ok() holds for, ok being monotone (holds up to some
    step, fails past it): a binary search. None when even the lowest fails."""
    lo, hi, found = 0, len(steps) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if ok(steps[mid]):
            found, lo = steps[mid], mid + 1
        else:
            hi = mid - 1
    return found


def readable(img: Image.Image, limit: float, q: float) -> bool:
    return y_percentile(img, q) <= limit


def best_frost(blurred: dict, bg: Image.Image, fade: int, limit: float) -> tuple[float, int, int] | None:
    """(score share, blur, brightness) of the most see-through readable frost
    at this fade. Blur is linear and the fade layer flat, so fading the
    pre-blurred image equals blurring the faded one; dimming only helps."""
    best = None
    for blur, img in blurred.items():
        base = Image.blend(img, bg, fade / PCT)
        bright = highest_passing(pct_steps(BRIGHTNESS_RANGE),
                                 lambda b: readable(ImageEnhance.Brightness(base).enhance(b / PCT), limit, FROST_PERCENTILE))
        if bright is None:
            continue
        share = bright / PCT * (1 - BLUR_COST * blur / BLUR_RANGE[1])
        if best is None or share > best[0]:
            best = (share, blur, bright)
    return best


def best_panel_fade(faded: Image.Image, surface: Image.Image, limit: float) -> int | None:
    """The most panel transparency still readable; None when even the floor isn't."""
    return highest_passing(pct_steps(PANEL_FADE_RANGE),
                           lambda p: readable(Image.blend(surface, faded, p / PCT), limit, PANEL_PERCENTILE))


def tune(img: Image.Image) -> dict:
    tokens = css_tokens()
    screen = ImageOps.fit(img, (SIM_W, SIM_H), Image.Resampling.LANCZOS)
    bg = Image.new("RGB", screen.size, tokens[FROST_SURFACE])
    panel = Image.new("RGB", screen.size, tokens[PANEL_SURFACE])
    frost_limit = max_backdrop_y(tokens, FROST_SURFACE)
    panel_limit = max_backdrop_y(tokens, PANEL_SURFACE)
    blurred = {b: screen.filter(ImageFilter.GaussianBlur(b * SIM_SCALE))
               for b in range(BLUR_RANGE[0], BLUR_RANGE[1] + 1)}
    best = None
    for fade in pct_steps(FADE_RANGE):
        frost = best_frost(blurred, bg, fade, frost_limit)
        if frost is None:
            continue
        share, blur, bright = frost
        pf = best_panel_fade(Image.blend(screen, bg, fade / PCT), panel, panel_limit)
        if pf is None:
            continue
        score = (1 - fade / PCT) * (FROST_WEIGHT * share + PANEL_WEIGHT * pf / PANEL_FADE_RANGE[1])
        if best is None or score > best[0]:
            best = (score, fade, blur, bright, pf)
    if best is None:
        # Nothing readable: the most covered it gets; flagged by the caller.
        return {"fade": FADE_RANGE[1], "blur": BLUR_RANGE[1], "brightness": BRIGHTNESS_RANGE[0],
                "panel_fade": PANEL_FADE_RANGE[0], "readable": False}
    _score, fade, blur, bright, pf = best
    return {"fade": fade, "blur": blur, "brightness": bright, "panel_fade": pf, "readable": True}


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
    img.save(out, quality=args.quality, method=WEBP_METHOD)

    preset_path = out.with_suffix(PRESET_EXT)
    if preset_path.exists() and not args.force:
        print(f"{name}: preset kept (--force to overwrite)")
        return
    t = tune(img)
    board, hue, colorful = pick_board(img)
    old = json.loads(preset_path.read_text(encoding="utf-8")) if preset_path.exists() else {}
    preset = {
        NAME_KEY: old.get(NAME_KEY, name),
        "fit": FIT,
        "fade": t["fade"],
        "frost": {"blur": t["blur"],
                  "brightness": min(BRIGHTNESS_RANGE[1], t["brightness"] + FROST_BRIGHTNESS_OFFSET)},
        "panels": {p: max(0, t["panel_fade"] + off) for p, off in PANELS.items()},
        "board": board,
        "boardFade": BOARD_FADE,
    }
    preset_path.write_text(json.dumps(preset, indent=2) + "\n", encoding="utf-8")
    flag = "" if t["readable"] else "  UNREADABLE: hand-tune"
    print(f"{name}: fade {t['fade']} blur {t['blur']} bright {t['brightness']} panels {t['panel_fade']}"
          f" board {board} (hue {hue * 360:.0f}, colorful {colorful:.2f}){flag}")


if __name__ == "__main__":
    main()
