// Themes: a bundled wallpaper plus a preset tuned for it. The server sets the
// image and the preset's fit/fade; this applies the rest: frost, panel and
// board fades here, the board style as the display setting. Keys a preset
// leaves out stay as they are; leaving a theme applies the defaults. Arrow
// colors ({ engine?, ai?, alt? }) are painted by theme-preset.js on the
// preset's board style while the theme is active.

import { boardFadePct, setBoardFade } from "./board-fade.js";
import { BOARD_STYLES, DEFAULT_BOARD_STYLE } from "./board-styles.js";
import { SETTINGS_PATH } from "./dialogs.js";
import { FROST_DEFAULTS, frostValues, setFrost } from "./frost.js";
import { FADE_DEFAULT_PCT, setPanelFades } from "./panel-fade.js";
import { loadJson } from "./storage.js";
import { PANEL_FADE_KEY } from "./theme-preset.js";
import { isObject } from "./wb-utils.js";

const storedBoardStyle = async (api) => (await api("GET", SETTINGS_PATH))?.board_style || DEFAULT_BOARD_STYLE;

// Stores a new board style; true when it changed.
async function putBoardStyle(board, api) {
  if (!Object.hasOwn(BOARD_STYLES, board)) return false;
  if (await storedBoardStyle(api) === board) return false;
  await api("PUT", SETTINGS_PATH, { board_style: board });
  return true;
}

// preset: { frost?: { blur?, brightness? }, panels?: { <panel name>: pct },
// board?: board style id, boardFade?: pct }. A new board style reloads the
// page, as the Settings dialog does, so the live board restyles; returns
// whether it does.
export async function applyThemePreset({ frost, panels, board, boardFade }, api) {
  const reload = await putBoardStyle(board, api);
  if (isObject(frost)) setFrost(frost);
  setBoardFade(boardFade);
  if (isObject(panels)) {
    setPanelFades(Object.fromEntries(Object.entries(panels)
      .filter(([name]) => Object.hasOwn(PANEL_FADE_KEY, name))
      .map(([name, pct]) => [PANEL_FADE_KEY[name], pct])));
  }
  if (reload) location.reload();
  return reload;
}

// The defaults as a preset; no board key, so the board style stays.
const DEFAULT_PRESET = Object.freeze({
  frost: FROST_DEFAULTS,
  panels: Object.fromEntries(Object.keys(PANEL_FADE_KEY).map((name) => [name, FADE_DEFAULT_PCT])),
  boardFade: FADE_DEFAULT_PCT,
});

export function clearThemePreset(api) {
  return applyThemePreset(DEFAULT_PRESET, api);
}

// True when a value the preset sets differs now; style is the wallpaper's
// current { fit, fade }.
export async function isThemeModified({ fit, fade, frost, panels, board, boardFade }, style, api) {
  const differs = (want, have) => want !== undefined && want !== have;
  const frostNow = frostValues();
  return differs(fit, style.fit) || differs(fade, style.fade) || differs(boardFade, boardFadePct())
    || (isObject(frost) && Object.keys(frost).some((key) => differs(frost[key], frostNow[key])))
    || (isObject(panels) && Object.entries(panels).some(([name, pct]) =>
      Object.hasOwn(PANEL_FADE_KEY, name) && differs(pct, loadJson(PANEL_FADE_KEY[name], FADE_DEFAULT_PCT))))
    // Checked first: a preset without a board skips the settings fetch.
    || (board !== undefined && differs(board, await storedBoardStyle(api)));
}
