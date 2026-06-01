// Window-level CustomEvent name constants for cross-module UI signals.
// A typo in a raw "sturddle:..." string fails silently (dead listener),
// so dispatch and listen sites share these names from one source.

export const APP_EVT = {
  LAYOUT_CHANGED: "sturddle:layout-changed",
  SETTINGS_CHANGED: "sturddle:settings-changed",
  LIVEGAME_CLOSED: "sturddle:livegame-closed",
};
