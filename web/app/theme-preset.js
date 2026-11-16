// The active theme's preset, kept in the browser so the panel menus' Resets
// restore the theme's values, not the built-in defaults. Stored when a theme is
// picked; dropped once the wallpaper info names another theme or none.

import { BOARD_STYLE_ATTR, BOARD_STYLES } from "./board-styles.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadJson, removeKey, saveJson } from "./storage.js";
import { isObject } from "./wb-utils.js";

// A preset's panel names and the fade each sets.
export const PANEL_FADE_KEY = Object.freeze({
  ai: STORAGE_KEY.AI_FADE,
  evalbar: STORAGE_KEY.EVALBAR_FADE,
  ucilog: STORAGE_KEY.UCILOG_FADE,
  pvtable: STORAGE_KEY.PVTABLE_FADE,
  studioLeft: STORAGE_KEY.STUDIO_LEFT_FADE,
  studioRight: STORAGE_KEY.STUDIO_RIGHT_FADE,
});

// A preset's arrow names and the board property each sets; the board styles'
// arrow colors fall back to their own when one is unset.
const ARROW_PROP = Object.freeze({
  engine: "--theme-arrow",
  ai: "--theme-arrow-secondary",
  alt: "--theme-arrow-alt",
});

// Holds the theme's arrow colors, scoped to the theme's board style: any other
// board keeps its own.
const arrowSheet = new CSSStyleSheet();
document.adoptedStyleSheets = [...document.adoptedStyleSheets, arrowSheet];

const isEntry = (v) => isObject(v) && typeof v.id === "string" && v.id !== "" && isObject(v.preset);

function paintArrows() {
  const preset = active?.preset;
  const arrows = isObject(preset?.arrows) ? preset.arrows : {};
  const known = Object.hasOwn(BOARD_STYLES, preset?.board);
  const decls = Object.entries(ARROW_PROP)
    .filter(([name]) => typeof arrows[name] === "string" && CSS.supports("color", arrows[name]))
    .map(([name, prop]) => `${prop}: ${arrows[name]};`);
  arrowSheet.replaceSync(known && decls.length
    ? `[${BOARD_STYLE_ATTR}="${preset.board}"] { ${decls.join(" ")} }` : "");
}

let active = loadJson(STORAGE_KEY.THEME_PRESET);
if (!isEntry(active)) active = null;
paintArrows();

export function rememberThemePreset(id, preset) {
  active = { id, preset };
  if (isEntry(active)) saveJson(STORAGE_KEY.THEME_PRESET, active);
  else {
    active = null;
    removeKey(STORAGE_KEY.THEME_PRESET);
  }
  paintArrows();
}

// The wallpaper info's current theme id (null: none).
export function syncThemePreset(id) {
  if (active && active.id !== id) rememberThemePreset(null);
}

// The theme's frost field, or undefined.
export const themeFrost = (key) => {
  const frost = active?.preset.frost;
  return isObject(frost) ? frost[key] : undefined;
};

// The theme's fade for a fader's storage key, or undefined.
export function themePanelFade(storageKey) {
  const panels = active?.preset.panels;
  if (!isObject(panels)) return undefined;
  const name = Object.keys(PANEL_FADE_KEY).find((n) => PANEL_FADE_KEY[n] === storageKey);
  return name ? panels[name] : undefined;
}
