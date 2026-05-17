// Dockable PGN-commentary window for play perspective view-mode.
//
// Reuses the createDockableWindow factory but with its own dock container
// (.play-comments-host) -- independent of the debug dock so commentary
// stacking is not coupled to the debug-window splitter logic.
//
// Lifecycle is driven by play.js: when view-mode is active and the user
// setting is enabled (and viewport isn't narrow), the window opens in its
// last-known mode (docked or floating). Per-ply text updates via
// setCommentaryText() -- no comment at the current ply shows a placeholder
// rather than closing the window. Exiting view-mode closes it.
// Closing the window (dock X or float X) clears the user setting via the
// onUserClose callback -- play.js then PUTs the new setting.

import { createDockableWindow, registerExtraDock } from "./play-debug-windows.js";

const GEO_KEY       = "sturddle:commentary:geo";
const WIN_STATE_KEY = "sturddle:commentary:winstate";
const DOCKED_KEY    = "sturddle:commentary:docked";
const OPEN_KEY      = "sturddle:commentary:open";
const EMPTY_TEXT    = "No commentary at this ply.";

let dockEl = null;
let unregisterDock = null;
let body = null;
let userCloseHandler = null;

function buildBody() {
  const root = document.createElement("div");
  root.className = "pgn-comments-body";
  root.tabIndex = 0;
  setText(root, null);
  return root;
}

function setText(el, text) {
  el.innerHTML = "";
  if (text) {
    el.classList.remove("is-empty");
    for (const para of text.split(/\n{2,}/)) {
      const p = document.createElement("p");
      p.textContent = para;
      el.append(p);
    }
  } else {
    el.classList.add("is-empty");
    const p = document.createElement("p");
    p.textContent = EMPTY_TEXT;
    el.append(p);
  }
}

const inst = createDockableWindow({
  title: "Commentary",
  className: "sturddle-wb-commentary",
  geoKey: GEO_KEY,
  winStateKey: WIN_STATE_KEY,
  dockedKey: DOCKED_KEY,
  openKey: OPEN_KEY,
  defaultW: () => 360,
  defaultH: 280,
  defaultY: () => 80,
  build() {
    body = buildBody();
    return body;
  },
  dockOrder: 10,
  getDockEl: () => dockEl,
  closable: true,
  onUserClose: () => {
    body = null;
    if (userCloseHandler) userCloseHandler();
  },
});

export function setOnUserCloseCommentary(fn) {
  userCloseHandler = fn;
}

export function setCommentaryDockContainer(el) {
  if (unregisterDock) { unregisterDock(); unregisterDock = null; }
  dockEl = el;
  if (el) unregisterDock = registerExtraDock(el);
}

export function openCommentary() {
  if (inst.wb || inst.slot) return;
  inst.toggle(null);
}

export function closeCommentary() {
  inst.close();
  // Factory clears its internal body ref on close; mirror that so a stale
  // setCommentaryText() after close doesn't write into a detached node.
  body = null;
}

export function setCommentaryText(text) {
  if (!body) return;
  setText(body, text);
}

export function isCommentaryOpen() {
  return !!(inst.wb || inst.slot);
}
