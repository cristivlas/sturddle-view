// Watch controls for a live-board row: an eye-slash that closes the board and
// an eye that opens it. Shared by the Arena workspace and Studio, which differ
// only in how they open a board (passed in as onWatch).

import { closeLiveWindow, isLiveWindowOpen } from "./tournament-live-game.js";

const WATCH_CLASS = "wb-sched-attach-btn";
const UNWATCH_CLASS = "wb-sched-unwatch-btn";
const LIVE_CLASS = "wb-sched-attach-btn--live";
const WATCH_ICON = "eye";
const UNWATCH_ICON = "eye-slash";
const WATCH_LABEL = "Watch";
const UNWATCH_LABEL = "Stop watching";
// Both kinds carry the key, so a refresh can resync a whole pane by query.
const CONTROL_SEL = `.${WATCH_CLASS}, .${UNWATCH_CLASS}`;

function makeBtn(className, icon, label, attachKey) {
  const btn = document.createElement("button");
  btn.className = className;
  btn.innerHTML = `<wa-icon name="${icon}"></wa-icon>`;
  btn.title = label;
  btn.setAttribute("aria-label", label);
  btn.dataset.attachKey = attachKey;
  return btn;
}

// Append [eye-slash][eye] to `parent`. onWatch(btn) opens the board.
export function appendWatchControls(parent, attachKey, onWatch) {
  const stop = makeBtn(UNWATCH_CLASS, UNWATCH_ICON, UNWATCH_LABEL, attachKey);
  stop.addEventListener("click", () => closeLiveWindow(attachKey));
  const watch = makeBtn(WATCH_CLASS, WATCH_ICON, WATCH_LABEL, attachKey);
  watch.addEventListener("click", () => onWatch(watch));
  parent.append(stop, watch);
  syncControl(stop);
  syncControl(watch);
  return watch;
}

function syncControl(btn) {
  const live = isLiveWindowOpen(btn.dataset.attachKey);
  if (btn.classList.contains(UNWATCH_CLASS)) btn.disabled = !live;
  else btn.classList.toggle(LIVE_CLASS, live);
}

// Resync every control under the given roots to the set of open boards.
export function refreshWatchControls(...roots) {
  for (const root of roots) {
    if (!root) continue;
    for (const btn of root.querySelectorAll(CONTROL_SEL)) syncControl(btn);
  }
}
