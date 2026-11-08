// Themes: a bundled wallpaper plus a preset tuned for it. The server sets the
// image and the preset's fit/fade; this applies the rest: frost, panel and
// board fades here, the board style as the display setting. Keys a preset
// leaves out stay as they are.

import { setBoardFade } from "./board-fade.js";
import { BOARD_STYLES, DEFAULT_BOARD_STYLE } from "./board-styles.js";
import { SETTINGS_PATH } from "./dialogs.js";
import { setFrost } from "./frost.js";
import { setPanelFades } from "./panel-fade.js";
import { PANEL_FADE_KEY } from "./theme-preset.js";
import { isObject } from "./wb-utils.js";

// Stores a new board style; true when it changed.
async function putBoardStyle(board, api) {
  if (!Object.hasOwn(BOARD_STYLES, board)) return false;
  const settings = await api("GET", SETTINGS_PATH);
  if ((settings?.board_style || DEFAULT_BOARD_STYLE) === board) return false;
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
