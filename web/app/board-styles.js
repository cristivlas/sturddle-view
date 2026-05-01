// Shared board style presets. id -> { label, cssClass, piecesFile }.
// Server-side whitelist must match the keys here (see api/settings.py).
export const BOARD_STYLES = {
  "classic":         { label: "Classic",         cssClass: "default",          piecesFile: "pieces/standard.svg" },
  "classic-staunty": { label: "Classic Staunty", cssClass: "default",          piecesFile: "pieces/staunty.svg"  },
  "green":           { label: "Green",           cssClass: "green",            piecesFile: "pieces/standard.svg" },
  "green-staunty":   { label: "Green Staunty",   cssClass: "green",            piecesFile: "pieces/staunty.svg"  },
  "blue":            { label: "Blue",            cssClass: "blue",             piecesFile: "pieces/standard.svg" },
  "chess-club":      { label: "Chess Club",      cssClass: "chess-club",       piecesFile: "pieces/staunty.svg"  },
  "black-and-white": { label: "Black & White",   cssClass: "black-and-white",  piecesFile: "pieces/standard.svg" },
  "high-contrast":   { label: "High Contrast",   cssClass: "default-contrast", piecesFile: "pieces/standard.svg" },
};

export const DEFAULT_BOARD_STYLE = "classic";

export function resolveBoardStyle(id) {
  return BOARD_STYLES[id] || BOARD_STYLES[DEFAULT_BOARD_STYLE];
}
