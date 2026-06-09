// Window-level CustomEvent name constants for cross-module UI signals.
// A typo in a raw "sturddle:..." string fails silently (dead listener),
// so dispatch and listen sites share these names from one source.

export const APP_EVT = {
  LAYOUT_CHANGED: "sturddle:layout-changed",
  SETTINGS_CHANGED: "sturddle:settings-changed",
  LIVEGAME_CLOSED: "sturddle:livegame-closed",
  // Active ribbon element changed (perspective mount/unmount); detail.el.
  RIBBON_ACTIVE: "sturddle:ribbon-active",
  // User closed the floating ribbon WinBox; revert to last docked side.
  RIBBON_FLOAT_CLOSED: "sturddle:ribbon-float-closed",
  // Floating ribbon WinBox was dragged; open edit-mode popovers close.
  RIBBON_MOVED: "sturddle:ribbon-moved",
  // WS connection up/down; detail.connected.
  CONNECTION: "sturddle:connection",
  // Engine registry changed; play perspective re-reads it.
  ENGINES_CHANGED: "sturddle:engines-changed",
  // Recent imports changed; play perspective refreshes its list.
  RECENTS_CHANGED: "sturddle:recents-changed",
  // Request top-nav perspective switch; detail.id.
  ACTIVATE_PERSPECTIVE: "sturddle:activate-perspective",
  // View mode entered/left; renames the Play tab. detail.viewing.
  VIEWING_CHANGED: "sturddle:viewing-changed",
  // A live game was reconciled against the tournament store.
  RECONCILED: "sturddle:reconciled",
  // A tournament workspace window closed.
  WORKSPACE_CLOSED: "sturddle:workspace-closed",
  // Deep-link request to open the settings dialog; detail.tab.
  OPEN_SETTINGS: "sturddle:open-settings",
  // Application log line appended; detail = formatted string.
  LOG: "sturddle:log",
};
