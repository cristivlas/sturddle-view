// Themes: a bundled wallpaper plus a preset tuned for it. The server sets the
// image and the preset's fit/fade; this applies the preset's client-side
// settings. Keys a preset leaves out stay as they are.

import { setFrost } from "./frost.js";
import { setPanelFades } from "./panel-fade.js";
import { STORAGE_KEY } from "./storage-keys.js";

// A preset's panel names and the fade each sets.
const PANEL_FADE_KEY = Object.freeze({
  ai: STORAGE_KEY.AI_FADE,
  evalbar: STORAGE_KEY.EVALBAR_FADE,
  ucilog: STORAGE_KEY.UCILOG_FADE,
  pvtable: STORAGE_KEY.PVTABLE_FADE,
  studioLeft: STORAGE_KEY.STUDIO_LEFT_FADE,
  studioRight: STORAGE_KEY.STUDIO_RIGHT_FADE,
});

const isObject = (v) => v !== null && typeof v === "object";

// preset: { frost?: { blur?, brightness? }, panels?: { <panel name>: pct } }.
export function applyThemePreset({ frost, panels }) {
  if (isObject(frost)) setFrost(frost);
  if (isObject(panels)) {
    setPanelFades(Object.fromEntries(Object.entries(panels)
      .filter(([name]) => Object.hasOwn(PANEL_FADE_KEY, name))
      .map(([name, pct]) => [PANEL_FADE_KEY[name], pct])));
  }
}
