// Dockable PGN-commentary window for play perspective view-mode.
//
// Docks into the shared left dock alongside the debug windows, so commentary
// and an open debug panel are visible at the same time. DOCK_ORDER.COMMENTARY
// sorts it above them.
//
// Lifecycle is driven by play.js: when view-mode is active and the user
// setting is enabled (and viewport isn't narrow), the window opens in its
// last-known mode (docked or floating). Per-ply text updates via
// setCommentaryText() -- no comment at the current ply shows a placeholder
// rather than closing the window. Exiting view-mode closes it.
// Closing the window (dock X or float X) clears the user setting via the
// handler play.js installs with commentaryWindow.setOnUserClose.

import { createDockableWindow, DOCK_ORDER } from "./play-dock-windows.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { markSelectable } from "./wb-utils.js";

const GEO_KEY       = STORAGE_KEY.COMMENTARY_GEO;
const WIN_STATE_KEY = STORAGE_KEY.COMMENTARY_WIN_STATE;
const DOCKED_KEY    = STORAGE_KEY.COMMENTARY_DOCKED;
const OPEN_KEY      = STORAGE_KEY.COMMENTARY_OPEN;
const EMPTY_TEXT    = "No commentary at this ply.";

let onNavPrev = null;
let onNavNext = null;

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
  markSelectable(root);
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

// Exported for its setOnUserClose: play.js owns what an X means (PUT the
// setting off), and only while it is mounted.
export const commentaryWindow = createDockableWindow({
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
  railDockable: true,
  // play.js's syncCommentsVisibility owns when this opens (view mode + user
  // setting + not mobile); restoreDebugWindows must not reopen it behind that.
  selfManaged: true,
  closable: true,
});

export function openCommentary() {
  if (commentaryWindow.mounted) return;
  commentaryWindow.toggle(null);
}

export function closeCommentary() {
  commentaryWindow.close();
}

export function setCommentaryText(text) {
  if (!commentaryWindow.body) return;
  setText(commentaryWindow.body, text);
}

export function setCommentaryNavHandlers(prev, next) {
  onNavPrev = prev;
  onNavNext = next;
}

export function setCommentaryNavState(prevPly, nextPly) {
  if (!commentaryWindow.body) return;
  commentaryWindow.body._prevBtn.disabled = prevPly == null;
  commentaryWindow.body._nextBtn.disabled = nextPly == null;
}

export function isCommentaryOpen() {
  return commentaryWindow.mounted;
}
