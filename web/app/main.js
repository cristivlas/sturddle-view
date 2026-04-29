import { connect } from "./ws.js";
import { PerspectiveRouter } from "./perspectives.js";
import { playPerspective } from "./perspectives/play.js";
import { observePerspective } from "./perspectives/observe.js";
import { openSettingsDialog } from "./settings-dialog.js";

const params = new URLSearchParams(location.search);
const token = params.get("token") || "";

const conn = document.getElementById("conn-status");
const nav = document.getElementById("perspective-nav");
const root = document.getElementById("perspective-root");

async function api(method, path, body) {
  const sep = path.includes("?") ? "&" : "?";
  const url = token ? `${path}${sep}token=${encodeURIComponent(token)}` : path;
  const r = await fetch(url, {
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
router.register(observePerspective);

function renderNav() {
  nav.innerHTML = "";
  for (const p of router.list()) {
    const btn = document.createElement("button");
    btn.dataset.perspective = p.id;
    btn.textContent = p.label;
    btn.addEventListener("click", async () => {
      await router.activate(p.id);
      renderNav();
    });
    if (p.id === router.activeId()) btn.classList.add("active");
    nav.appendChild(btn);
  }
}

connect({
  token,
  onOpen: () => {
    conn.classList.add("connected");
    conn.title = "connected";
  },
  onClose: () => {
    conn.classList.remove("connected");
    conn.title = "disconnected";
  },
  onEvent: (evt) => events.emit(evt),
});

document.getElementById("settings-btn").addEventListener("click", () => {
  openSettingsDialog({ api });
});

await router.activateInitial();
renderNav();
