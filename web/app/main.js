import { connect } from "./ws.js";
import { PerspectiveRouter } from "./perspectives.js";
import { playPerspective } from "./perspectives/play.js";
import { enginesPerspective } from "./perspectives/engines.js";
import { openSettingsDialog } from "./settings-dialog.js";
import { openAboutDialog } from "./about-dialog.js";
import { openRibbonWindow, closeRibbonWindow, mountRibbonElement, isRibbonFloating, nudgeRibbonToViewport, RIBBON_SIDE_KEY } from "./ribbon-window.js";
import { loadRaw, saveRaw } from "./storage.js";
import { mqMobile } from "./breakpoints.js";
import { APP_EVT } from "./app-events.js";

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
function renderNav() {
  nav.innerHTML = "";
  for (const p of router.list()) {
    const btn = document.createElement("button");
    btn.dataset.perspective = p.id;
    btn.textContent = p.id === "play" && inViewMode ? "View" : p.label;
    btn.addEventListener("click", async () => {
      await router.activate(p.id);
      renderNav();
    });
    if (p.id === router.activeId()) btn.classList.add("active");
    nav.appendChild(btn);
  }
}
window.addEventListener(APP_EVT.VIEWING_CHANGED, (ev) => {
  inViewMode = !!ev.detail?.viewing;
  renderNav();
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
}).catch(() => {});

document.getElementById("about-btn").addEventListener("click", () => {
  openAboutDialog({ api });
});
const reloadPerspective = () => router.activate(router.activeId(), { force: true });
document.getElementById("settings-btn").addEventListener("click", () => {
  openSettingsDialog({ api, getActivePerspective: () => router.activeId(), reloadPerspective });
});
// Allow any module to deep-link into the Settings dialog without
// threading the `api` reference through call chains. detail.tab opens
// the named tab (e.g. "engines"). Used by the Play empty-state CTA and
// the no-engine error toast.
window.addEventListener(APP_EVT.OPEN_SETTINGS, (e) => {
  const tab = e.detail?.tab;
  openSettingsDialog({ api, initialTab: tab, getActivePerspective: () => router.activeId(), reloadPerspective });
});

window.addEventListener(APP_EVT.CONNECTION, async (e) => {
  if (e.detail.connected) return;
  if (router.activeId() === "engines") {
    await router.activate("play");
    renderNav();
  }
});

window.addEventListener(APP_EVT.ACTIVATE_PERSPECTIVE, async (e) => {
  const id = e.detail?.id;
  if (!id) return;
  try {
    if (router.activeId() !== id) {
      await router.activate(id);
      renderNav();
    }
  } catch (err) {
    console.error(`activate-perspective(${id}) failed`, err);
  }
});

await router.activateInitial();
renderNav();
