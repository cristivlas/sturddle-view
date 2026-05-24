// Dockable AI analysis prose window. Mirrors play-commentary-window.js
// but consumes `ai_info` websocket events to stream coach commentary as
// the agent emits it.
//
// Skeleton scope: opens on first chunk, appends every `ai_info` delta,
// finishes when payload carries done=true (and shows a "cancelled" marker
// if done && cancelled). Lifecycle (open/close on mode entry, persistence
// of user-pinned state) is driven by play.js -- this module only owns the
// dom inside the window.

import { createDockableWindow, registerExtraDock } from "./play-debug-windows.js";

const GEO_KEY       = "sturddle:ai:geo";
const WIN_STATE_KEY = "sturddle:ai:winstate";
const DOCKED_KEY    = "sturddle:ai:docked";
const OPEN_KEY      = "sturddle:ai:open";
const EMPTY_TEXT    = "No AI analysis yet.";

let dockEl = null;
let unregisterDock = null;
let userCloseHandler = null;

function buildBody() {
  const root = document.createElement("div");
  root.className = "play-ai-body";
  root.tabIndex = 0;
  const para = document.createElement("p");
  para.className = "play-ai-prose";
  para.textContent = EMPTY_TEXT;
  root.append(para);
  root._para = para;
  root._hasContent = false;
  return root;
}

const inst = createDockableWindow({
  title: "AI Analysis",
  className: "sturddle-wb-ai",
  geoKey: GEO_KEY,
  winStateKey: WIN_STATE_KEY,
  dockedKey: DOCKED_KEY,
  openKey: OPEN_KEY,
  defaultW: () => 380,
  defaultH: 300,
  defaultY: () => 100,
  build() {
    return buildBody();
  },
  dockOrder: 20,
  getDockEl: () => dockEl,
  closable: true,
  onUserClose: () => {
    if (userCloseHandler) userCloseHandler();
  },
});

export function setOnUserCloseAi(fn) {
  userCloseHandler = fn;
}

export function setAiDockContainer(el) {
  if (unregisterDock) { unregisterDock(); unregisterDock = null; }
  dockEl = el;
  if (el) unregisterDock = registerExtraDock(el);
}

export function openAi() {
  if (inst.wb || inst.slot) return;
  inst.toggle(null);
}

export function closeAi() {
  inst.close();
}

export function isAiOpen() {
  return !!(inst.wb || inst.slot);
}

export function resetAi() {
  if (!inst.body) return;
  inst.body._para.textContent = EMPTY_TEXT;
  inst.body._hasContent = false;
}

export function appendAiDelta(text) {
  if (!inst.body || !text) return;
  if (!inst.body._hasContent) {
    inst.body._para.textContent = "";
    inst.body._hasContent = true;
  }
  inst.body._para.append(document.createTextNode(text));
}

export function markAiDone({ cancelled = false } = {}) {
  if (!inst.body || !cancelled) return;
  const marker = document.createElement("span");
  marker.className = "play-ai-cancelled";
  marker.textContent = " [cancelled]";
  inst.body._para.append(marker);
}
