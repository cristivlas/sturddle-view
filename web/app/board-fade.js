// Board transparency: how much of the wallpaper shows through the board and
// the windows holding one (styles.css: only over a wallpaper). One setting
// app-wide, saved in the browser, set from the Appearance menu or a theme's
// preset.

import { FADE_DEFAULT_PCT, FADE_MAX_PCT, isFadePct } from "./panel-fade.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadJson, saveJson } from "./storage.js";
import { PCT_SUFFIX } from "./wb-utils.js";

const OPACITY_VAR = "--board-opacity";
const FULL_OPACITY_PCT = 100;
// Grid lines (styles.css) show only past this much transparency.
const BOARD_GRID_MIN_PCT = 70;
const GRID_ATTR = "data-board-grid";

const stored = loadJson(STORAGE_KEY.BOARD_FADE);
let pct = isFadePct(stored) ? stored : FADE_DEFAULT_PCT;

export const boardFadePct = () => pct;

// Paints pct live (a slider drag); saveBoardFade() keeps it.
export function paintBoardFade(next) {
  pct = next;
  const opacity = FULL_OPACITY_PCT * (1 - pct / FADE_MAX_PCT);
  document.documentElement.style.setProperty(OPACITY_VAR, `${opacity}${PCT_SUFFIX}`);
  document.documentElement.toggleAttribute(GRID_ATTR, pct > BOARD_GRID_MIN_PCT);
}
paintBoardFade(pct);

export const saveBoardFade = () => saveJson(STORAGE_KEY.BOARD_FADE, pct);

// Set and save (a theme's preset); an out-of-range pct is skipped.
export function setBoardFade(next) {
  if (!isFadePct(next)) return;
  paintBoardFade(next);
  saveBoardFade();
}
