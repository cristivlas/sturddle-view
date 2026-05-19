import { connect } from "./ws.js";
import { PerspectiveRouter } from "./perspectives.js";
import { playPerspective } from "./perspectives/play.js";
import { enginesPerspective } from "./perspectives/engines.js";
import { openSettingsDialog } from "./settings-dialog.js";
import { openAboutDialog } from "./about-dialog.js";

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
  window.dispatchEvent(new CustomEvent("sturddle:log", { detail: formatted }));
}

function getLogSnapshot() {
  return logBuffer.slice();
}

const ctx = { api, events, token, log, getLogSnapshot };

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
window.addEventListener("sturddle:viewing-changed", (ev) => {
  inViewMode = !!ev.detail?.viewing;
  renderNav();
});

function setConnected(yes) {
  conn.classList.toggle("connected", yes);
  conn.title = yes ? "connected" : "disconnected";
  document.body.classList.toggle("disconnected", !yes);
  window.dispatchEvent(
    new CustomEvent("sturddle:connection", { detail: { connected: yes } })
  );
}

connect({
  token,
  onOpen: () => setConnected(true),
  onClose: () => setConnected(false),
  onEvent: (evt) => events.emit(evt),
});

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
window.addEventListener("sturddle:open-settings", (e) => {
  const tab = e.detail?.tab;
  openSettingsDialog({ api, initialTab: tab, getActivePerspective: () => router.activeId(), reloadPerspective });
});

window.addEventListener("sturddle:connection", async (e) => {
  if (e.detail.connected) return;
  if (router.activeId() === "engines") {
    await router.activate("play");
    renderNav();
  }
});

window.addEventListener("sturddle:activate-perspective", async (e) => {
  const id = e.detail?.id;
  if (!id) return;
  try {
    if (router.activeId() !== id) {
      await router.activate(id);
      renderNav();
    }
    window.dispatchEvent(new CustomEvent("sturddle:perspective-activated", { detail: e.detail }));
  } catch (err) {
    console.error(`activate-perspective(${id}) failed`, err);
  }
});

await router.activateInitial();
renderNav();
