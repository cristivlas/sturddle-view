import { connect } from "./ws.js";
import { PerspectiveRouter } from "./perspectives.js";
import { playPerspective } from "./perspectives/play.js";
import { enginesPerspective } from "./perspectives/engines.js";
import { openSettingsDialog, migrateLegacyPlayerName } from "./settings-dialog.js";
import { openAboutDialog } from "./about-dialog.js";
import { openRibbonWindow, closeRibbonWindow, mountRibbonElement, isRibbonFloating, nudgeRibbonToViewport, RIBBON_SIDE_KEY } from "./ribbon-window.js";
import { loadRaw, saveRaw } from "./storage.js";
import { mqMobile } from "./breakpoints.js";
import { APP_EVT } from "./app-events.js";
import { getTournamentUx, tournamentUxLabel } from "./tournament-studio.js";
import { installSelection } from "./wb-utils.js";

installSelection();

// F11 toggles native fullscreen ("theater mode") in the desktop shell via the
// pywebview bridge -- the OS/browser F11 handling doesn't apply to the
// chromeless native window, so this is a no-op outside desktop mode.
window.addEventListener("keydown", (ev) => {
  if (ev.key !== "F11") return;
  const toggle = window.pywebview?.api?.toggle_fullscreen;
  if (!toggle) return;
  ev.preventDefault();
  toggle();
});

// Auth is carried by the HttpOnly cookie set during the /auth handshake.
const token = "";

const conn = document.getElementById("conn-status");
const nav = document.getElementById("perspective-nav");
const root = document.getElementById("perspective-root");

async function api(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`${method} ${path} -> ${r.status} ${detail}`);
  }
  if (r.status === 204) return null;
  return r.json();
}

// Tiny synchronous event bus. Perspectives subscribe on mount, return
// the unsubscribe function for cleanup.
function makeBus() {
  const subs = new Set();
  return {
    on(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
    emit(evt) {
      for (const fn of subs) {
        try {
          fn(evt);
        } catch (e) {
          console.error("event handler threw", e);
        }
      }
    },
  };
}

const events = makeBus();

// Application-wide log: keeps a bounded buffer of strings, dispatches a
// "sturddle:log" custom event when a new line arrives. Perspectives subscribe
// to render it in their debug panel.
const LOG_LIMIT = 500;
const logBuffer = [];

function log(line) {
  const ts = new Date().toISOString().substring(11, 19);
  const formatted = `${ts} ${line}`;
  logBuffer.push(formatted);
  if (logBuffer.length > LOG_LIMIT) logBuffer.shift();
  window.dispatchEvent(new CustomEvent(APP_EVT.LOG, { detail: formatted }));
}

function getLogSnapshot() {
  return logBuffer.slice();
}

const ctx = { api, events, token, log, getLogSnapshot };

// Ribbon side: stored in localStorage, drives [data-ribbon-side] and
// [data-ribbon-float] on <body>. "left"/"right" dock; "float" opens a WinBox.
let lastDockedSide = "left";
let mobileFloatSuppressed = false;

function applyRibbonSide(side) {
  const isFloat = side === "float";
  if (!isFloat) { lastDockedSide = side; mobileFloatSuppressed = false; }
  let changed = false;
  const wantSide = isFloat ? lastDockedSide : side;
  if (document.body.dataset.ribbonSide !== wantSide) {
    document.body.dataset.ribbonSide = wantSide;
    changed = true;
  }
  const floatAttr = isFloat ? "1" : "";
  if ((document.body.dataset.ribbonFloat ?? "") !== floatAttr) {
    if (isFloat) document.body.dataset.ribbonFloat = "1";
    else delete document.body.dataset.ribbonFloat;
    changed = true;
  }
  if (changed) window.dispatchEvent(new CustomEvent(APP_EVT.LAYOUT_CHANGED));
}

async function refreshRibbonSide() {
  const stored = loadRaw(RIBBON_SIDE_KEY);
  let side = stored || "left";
  // Always fetch the server's ribbon_side so lastDockedSide reflects the
  // user's left/right preference even when float is the current mode.
  try {
    const s = await api("GET", "/settings");
    if (s?.ribbon_side === "right" || s?.ribbon_side === "left") {
      lastDockedSide = s.ribbon_side;
      if (side !== "float") side = s.ribbon_side;
    }
  } catch { /* keep defaults */ }
  applyRibbonSide(side);
}
refreshRibbonSide();
window.addEventListener(APP_EVT.SETTINGS_CHANGED, refreshRibbonSide);

// Global float manager. Each perspective dispatches sturddle:ribbon-active
// with detail.el = the active ribbon element (or null on unmount). The
// manager mounts that element into the WinBox when data-ribbon-float is set.
let activeRibbon = null;

function syncFloatState() {
  const wantFloat = !!document.body.dataset.ribbonFloat || mobileFloatSuppressed;
  if (wantFloat && mqMobile.matches) {
    if (isRibbonFloating()) closeRibbonWindow();
    if (!mobileFloatSuppressed) {
      mobileFloatSuppressed = true;
      delete document.body.dataset.ribbonFloat;
    }
    return;
  }
  if (mobileFloatSuppressed) {
    mobileFloatSuppressed = false;
    document.body.dataset.ribbonFloat = "1";
  }
  if (wantFloat && activeRibbon) {
    if (!isRibbonFloating()) openRibbonWindow(activeRibbon);
    else mountRibbonElement(activeRibbon);
  } else if (isRibbonFloating()) {
    closeRibbonWindow();
  }
}
window.addEventListener(APP_EVT.RIBBON_ACTIVE, (e) => {
  activeRibbon = e.detail?.el ?? null;
  syncFloatState();
});
window.addEventListener(APP_EVT.LAYOUT_CHANGED, syncFloatState);
mqMobile.addEventListener("change", syncFloatState);
window.addEventListener("resize", () => {
  if (mqMobile.matches) syncFloatState();
  else nudgeRibbonToViewport();
});

// When the user closes the floating ribbon WinBox, revert to last docked side.
window.addEventListener(APP_EVT.RIBBON_FLOAT_CLOSED, () => {
  saveRaw(RIBBON_SIDE_KEY, lastDockedSide);
  delete document.body.dataset.ribbonFloat;
  window.dispatchEvent(new CustomEvent(APP_EVT.LAYOUT_CHANGED));
});

const router = new PerspectiveRouter({ root, ctx });
router.register(playPerspective);
router.register(enginesPerspective);

// View mode renames the "Play" tab to "View" so the active mode is
// unambiguous from the top-level nav. Toggled by play.js dispatching
// a sturddle:viewing-changed event.
let inViewMode = false;
// Tab a dropped socket pushed us off, pending restore on reconnect. Any
// deliberate pick clears it, so we never yank the user back to a tab they
// chose to leave while offline.
let tabLeftOnDisconnect = null;

// The one deliberate-switch path: persists the choice (activate's default)
// and cancels a pending disconnect-restore. Fast successive clicks coalesce
// last-wins in the router -- only the final target mounts, by design.
async function pickPerspective(id) {
  tabLeftOnDisconnect = null;
  await router.activate(id);
  renderNav();
}

function renderNav() {
  nav.innerHTML = "";
  for (const p of router.list()) {
    const btn = document.createElement("button");
    btn.dataset.perspective = p.id;
    // Tournaments tab tracks the UX-mode setting (Arena vs Studio).
    btn.textContent =
      p.id === "play" && inViewMode ? "View"
      : p.id === enginesPerspective.id ? tournamentUxLabel()
      : p.label;
    btn.addEventListener("click", () => pickPerspective(p.id));
    if (p.id === router.activeId()) btn.classList.add("active");
    nav.appendChild(btn);
  }
}
window.addEventListener(APP_EVT.VIEWING_CHANGED, (ev) => {
  inViewMode = !!ev.detail?.viewing;
  renderNav();
});
// UX-mode setting changed (Display tab) -- refresh the tab label, and if the
// Tournaments tab is live, remount it so the chosen shell (Arena/Studio)
// swaps in. Guarded on actual UX change so unrelated display edits don't
// tear down the workspace. force:true remounts the same id.
let lastUx = getTournamentUx();
window.addEventListener(APP_EVT.SETTINGS_CHANGED, async () => {
  renderNav();
  const ux = getTournamentUx();
  if (ux === lastUx) return;
  lastUx = ux;
  if (router.activeId() === enginesPerspective.id) {
    await router.activate(enginesPerspective.id, { force: true, persist: false });
    renderNav();
  }
});

function setConnected(yes) {
  conn.classList.toggle("connected", yes);
  conn.title = yes ? "connected" : "disconnected";
  document.body.classList.toggle("disconnected", !yes);
  window.dispatchEvent(
    new CustomEvent(APP_EVT.CONNECTION, { detail: { connected: yes } })
  );
}

connect({
  token,
  onOpen: () => setConnected(true),
  onClose: () => setConnected(false),
  onEvent: (evt) => events.emit(evt),
});

api("GET", "/settings").then(s => {
  const footer = document.getElementById("app-footer");
  if (footer && s) {
    const ver = s.version ? ` v${s.version}` : "";
    const copy = s.copyright ? ` -- (c) ${s.copyright}` : "";
    footer.textContent = `SturddleView${ver}${copy}`;
  }
  migrateLegacyPlayerName(api, s);
}).catch(() => {});

document.getElementById("about-btn").addEventListener("click", () => {
  openAboutDialog({ api });
});
// Remount of whatever is current, not a tab pick -- must not claim the
// startup slot, which during a disconnect window still holds the pre-drop tab.
const reloadPerspective = () => router.activate(router.activeId(), { force: true, persist: false });
document.getElementById("settings-btn").addEventListener("click", () => {
  openSettingsDialog({ api, getActivePerspective: () => router.activeId(), reloadPerspective });
});
// Allow any module to deep-link into the Settings dialog without
// threading the `api` reference through call chains. detail.tab opens
// the named tab (e.g. "engines"); detail.focus names a control within it
// to land on. Used by the Play empty-state CTA and error-toast gears.
window.addEventListener(APP_EVT.OPEN_SETTINGS, (e) => {
  const tab = e.detail?.tab;
  openSettingsDialog({
    api,
    initialTab: tab,
    focusClass: e.detail?.focus,
    getActivePerspective: () => router.activeId(),
    reloadPerspective,
  });
});

// A server-backed tab can't survive a dropped socket, so fall back to Play --
// then undo that once the socket is back. Both moves are involuntary, so
// neither rewrites the startup tab (persist: false).
window.addEventListener(APP_EVT.CONNECTION, async (e) => {
  if (e.detail.connected) {
    const id = tabLeftOnDisconnect;
    // Only restore if we're still parked where the fallback left us.
    if (!id || router.activeId() !== playPerspective.id) return;
    tabLeftOnDisconnect = null;
    await router.activate(id, { persist: false });
    renderNav();
    return;
  }
  if (router.activeId() === enginesPerspective.id) {
    tabLeftOnDisconnect = enginesPerspective.id;
    await router.activate(playPerspective.id, { persist: false });
    renderNav();
  }
});

window.addEventListener(APP_EVT.ACTIVATE_PERSPECTIVE, async (e) => {
  const id = e.detail?.id;
  if (!id) return;
  try {
    if (router.activeId() !== id) await pickPerspective(id);
  } catch (err) {
    console.error(`activate-perspective(${id}) failed`, err);
  }
});

await router.activateInitial();
renderNav();
