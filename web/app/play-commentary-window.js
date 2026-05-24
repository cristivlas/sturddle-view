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

import { createDockableWindow, DOCK_ORDER, registerExtraDock } from "./play-dock-windows.js";

const GEO_KEY       = "sturddle:commentary:geo";
const WIN_STATE_KEY = "sturddle:commentary:winstate";
const DOCKED_KEY    = "sturddle:commentary:docked";
const OPEN_KEY      = "sturddle:commentary:open";
const EMPTY_TEXT    = "No commentary at this ply.";

let onNavPrev = null;
let onNavNext = null;

let dockEl = null;
let unregisterDock = null;
let userCloseHandler = null;

function buildBody() {
  const root = document.createElement("div");
  root.className = "pgn-comments-body";
  root.tabIndex = 0;

  const nav = document.createElement("div");
  nav.className = "pgn-comments-nav";
  const prevBtn = document.createElement("button");
  prevBtn.type = "button";
  prevBtn.className = "pgn-comments-nav-btn";
  prevBtn.title = "Previous comment";
  prevBtn.setAttribute("aria-label", "Previous comment");
  prevBtn.innerHTML = `<wa-icon name="square-caret-left"></wa-icon>`;
  prevBtn.disabled = true;
  const nextBtn = document.createElement("button");
  nextBtn.type = "button";
  nextBtn.className = "pgn-comments-nav-btn";
  nextBtn.title = "Next comment";
  nextBtn.setAttribute("aria-label", "Next comment");
  nextBtn.innerHTML = `<wa-icon name="square-caret-right"></wa-icon>`;
  nextBtn.disabled = true;
  prevBtn.addEventListener("click", () => { if (onNavPrev) onNavPrev(); });
  nextBtn.addEventListener("click", () => { if (onNavNext) onNavNext(); });
  nav.append(prevBtn, nextBtn);
  root._prevBtn = prevBtn;
  root._nextBtn = nextBtn;

  root.append(nav);
  setText(root, null);
  return root;
}

function setText(el, text) {
  // Remove old paragraphs, keep the nav row.
  for (const p of Array.from(el.querySelectorAll("p"))) p.remove();
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
    return buildBody();
  },
  dockOrder: DOCK_ORDER.COMMENTARY,
  getDockEl: () => dockEl,
  closable: true,
  onUserClose: () => {
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
}

export function setCommentaryText(text) {
  if (!inst.body) return;
  setText(inst.body, text);
}

export function setCommentaryNavHandlers(prev, next) {
  onNavPrev = prev;
  onNavNext = next;
}

export function setCommentaryNavState(prevPly, nextPly) {
  if (!inst.body) return;
  inst.body._prevBtn.disabled = prevPly == null;
  inst.body._nextBtn.disabled = nextPly == null;
}

export function isCommentaryOpen() {
  return !!(inst.wb || inst.slot);
}
