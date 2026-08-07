// localStorage key constants. All persisted UI state keys live here under
// the "sturddle:" namespace so the prefix and every key string have one
// source of truth -- a typo in a raw key silently splits read from write.

export const STORAGE_KEY = {
  // File-picker last-directory and exe-filter state (prefix; per-purpose suffix appended).
  FS_PICKER_LAST_PREFIX: "sturddle:fs-picker:last:",
  FS_PICKER_EXE_ONLY_PREFIX: "sturddle:fs-picker:exeOnly:",

  // Engines list view state.
  ENGINES_COL_PCTS: "sturddle:engines:colPcts3",
  ENGINES_HEADER_SORT: "sturddle:engines:headerSort",
  // Legacy scalar name-sort, superseded by ENGINES_HEADER_SORT. Kept only so
  // the stale entry can be cleared on mount; safe to drop once users migrate.
  ENGINES_SORT_ORDER_LEGACY: "sturddle:engines:sortOrder",
  ENGINES_SETTINGS_COL_PCTS: "sturddle:engines:settings:colPcts3",

  // Import dialog recents cache.
  IMPORT_RECENTS: "sturddle:import:recent",
  OPENINGS_COL_PCTS: "sturddle:openings:colPcts3",
  OPENINGS_SORT: "sturddle:openings:sort",
  FS_PICKER_COL_PCTS: "sturddle:fs-picker:colPcts3",
  FS_PICKER_SORT: "sturddle:fs-picker:sort",

  // Active perspective + per-view flip.
  ACTIVE_PERSPECTIVE: "sturddle:active-perspective",
  VIEW_FLIPPED: "sturddle:view:flipped",

  // Ribbon docking + floating geometry.
  RIBBON_SIDE: "sturddle:ribbon:side",
  RIBBON_GEO: "sturddle:ribbon:geo",
  RIBBON_ORIENT: "sturddle:ribbon:orient",

  // Legacy player name (pre-0.5.2): now a server setting; kept only so
  // the one-time boot migration can read + clear it.
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

  // Play dock + UCI-log + PV-table + eval-strip window state.
  PLAY_DOCK_GROW: "sturddle:play:dockGrow",
  PLAY_DOCK_DEST: "sturddle:play:dockDest",
  PLAY_RAIL_LIFT: "sturddle:play:railLift",
  EVALBAR_GEO: "sturddle:evalbar:geo",
  EVALBAR_WIN_STATE: "sturddle:evalbar:winstate",
  EVALBAR_DOCKED: "sturddle:evalbar:docked",
  EVALBAR_OPEN: "sturddle:evalbar:open",
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
  TOURNAMENT_UX: "sturddle:tournament:ux",
  STUDIO_SPLIT_ROW: "sturddle:studio:splitRow",
  STUDIO_SPLIT_COL: "sturddle:studio:splitCol",
  STUDIO_SELECTED_ID: "sturddle:studio:selectedId",
  STUDIO_BOARDS_PREFIX: "sturddle:studio:boards:",
  STUDIO_TAB_LEFT: "sturddle:studio:tabLeft",
  STUDIO_TAB_RIGHT: "sturddle:studio:tabRight",
  STUDIO_TOURNEY_SORT: "sturddle:studio:tourneySort",
  STUDIO_TOURNEY_STACK: "sturddle:studio:tourneyStack",
  STUDIO_TOURNEY_COL_PCTS: "sturddle:studio:tourneyColPcts",
  STUDIO_HISTORY_SORT: "sturddle:studio:historySort",
  STUDIO_HISTORY_STACK: "sturddle:studio:historyStack",
  STUDIO_HISTORY_COL_PCTS: "sturddle:studio:historyColPcts",
  STUDIO_H2H_COL_PCTS: "sturddle:studio:h2hColPcts",
  STUDIO_ENGINES_SORT: "sturddle:studio:enginesSort",
  WORKSPACE_PREFIX: "sturddle:workspace:",
  LIVE_PVTABLE_COL_WIDTHS: "sturddle:live:pvtable:colWidths",
  TOURNAMENTS_STANDINGS_COL_PCTS: "sturddle:tournaments:standingsColPcts",
  TOURNAMENTS_SORT_BY: "sturddle:tournaments:sortBy",
  TOURNAMENTS_SORT_ASC: "sturddle:tournaments:sortAsc",
};
