// localStorage key constants. All persisted UI state keys live here under
// the "sturddle:" namespace so the prefix and every key string have one
// source of truth -- a typo in a raw key silently splits read from write.

export const STORAGE_KEY = {
  // File-picker last-directory (prefix; per-purpose suffix appended).
  FS_PICKER_LAST_PREFIX: "sturddle:fs-picker:last:",

  // Engines list view state.
  ENGINES_COL_PCTS: "sturddle:engines:colPcts3",
  ENGINES_SORT_ORDER: "sturddle:engines:sortOrder",
  ENGINES_SETTINGS_COL_PCTS: "sturddle:engines:settings:colPcts3",

  // Import dialog recents cache.
  IMPORT_RECENTS: "sturddle:import:recent",

  // Active perspective + per-view flip.
  ACTIVE_PERSPECTIVE: "sturddle:active-perspective",
  VIEW_FLIPPED: "sturddle:view:flipped",

  // Ribbon docking + floating geometry.
  RIBBON_SIDE: "sturddle:ribbon:side",
  RIBBON_GEO: "sturddle:ribbon:geo",
  RIBBON_ORIENT: "sturddle:ribbon:orient",

  // Player name.
  PLAYER_NAME: "sturddle:player_name",

  // AI window state.
  AI_GEO: "sturddle:ai:geo",
  AI_WIN_STATE: "sturddle:ai:winstate",
  AI_DOCKED: "sturddle:ai:docked",
  AI_OPEN: "sturddle:ai:open",
  AI_TITLE_MODEL: "sturddle:ai:title-model",
  AI_THINKING_OPEN: "sturddle:ai:thinking-open",

  // Commentary window state.
  COMMENTARY_GEO: "sturddle:commentary:geo",
  COMMENTARY_WIN_STATE: "sturddle:commentary:winstate",
  COMMENTARY_DOCKED: "sturddle:commentary:docked",
  COMMENTARY_OPEN: "sturddle:commentary:open",

  // Play dock + UCI-log + PV-table window state.
  PLAY_DOCK_GROW: "sturddle:play:dockGrow",
  UCILOG_GEO: "sturddle:ucilog:geo",
  UCILOG_WIN_STATE: "sturddle:ucilog:winstate",
  UCILOG_DOCKED: "sturddle:ucilog:docked",
  UCILOG_OPEN: "sturddle:ucilog:open",
  PVTABLE_GEO: "sturddle:pvtable:geo",
  PVTABLE_WIN_STATE: "sturddle:pvtable:winstate",
  PVTABLE_DOCKED: "sturddle:pvtable:docked",
  PVTABLE_OPEN: "sturddle:pvtable:open",
  PVTABLE_COL_WIDTHS: "sturddle:pvtable:colWidths",
  VIEW_UCILOG_OPEN: "sturddle:view:ucilog:open",
  VIEW_PVTABLE_OPEN: "sturddle:view:pvtable:open",

  // Tournament workspace + list state.
  WORKSPACE_PREFIX: "sturddle:workspace:",
  TOURNAMENTS_STANDINGS_COL_PCTS: "sturddle:tournaments:standingsColPcts",
  ACTIVE_LAYOUT: "sturddle:active-layout",
  TOURNAMENTS_SORT_BY: "sturddle:tournaments:sortBy",
  TOURNAMENTS_SORT_ASC: "sturddle:tournaments:sortAsc",
};
