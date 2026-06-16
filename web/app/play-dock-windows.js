// Dockable windows for play mode (desktop only).
// 1. Search Lines: per-iteration principal variation, cutechess-style.
// 2. AI Analysis: streamed prose commentary from the AI agent.
// 3. UCI log: raw lines flowing between python-chess and the engine.
//
// Each window can float (WinBox) or dock into the left column of the play
// grid (.play-dock-left). Dock state is persisted in localStorage; when
// two or more windows are docked, drag-grips between adjacent slots
// resize them (per-slot flex-grow ratios stored in DOCK_GROW_KEY).
//
// The exported createDockableWindow factory is reused by play-commentary-
// window.js, which supplies its own dock container (.play-comments-host)
// via getDockEl. Such instances are flagged !usesMainDock so main-dock
// lifecycle helpers (closeDebugWindows, restoreDebugWindows) skip them.
//
// Narrow-viewport behavior: at <=800px width / <=700px height, CSS hides
// .play-dock-left. The JS still creates dock slots into the (hidden)
// container on restore/toggle. This is intentional: it's slightly wasteful
// (a few DOM nodes + listeners) but gives free recovery when the user
// resizes back to desktop -- their docked windows reappear with content
// intact. Short-circuiting toggle()/restore() at narrow widths was
// considered but rejected for the UX regressions (broken toggle buttons,
// no resize-back recovery, divergent localStorage state).

import { toast } from "./dialogs.js";
import { mqMobile, mqMobileHPlay } from "./breakpoints.js";
import { APP_EVT } from "./app-events.js";
import { KIND } from "./game-events.js";
import { createPvTable } from "./pv-table.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadJson, saveJson, loadRaw, saveRaw } from "./storage.js";
import {
  AUTOSCROLL_SLACK_LINE_PX,
  isPinnedToBottom,
  rafCoalesce,
  ribbonWidthPx,
  scrollToBottom,
} from "./wb-utils.js";

const PLAY_GRID_SEL = ".play-grid";

// Vertical stack order for docked windows. Lower values render higher
// in the column. Centralized so adding a new window doesn't require
// guessing an unused number; the gaps between values leave room for
// future insertions without renumbering existing entries.
export const DOCK_ORDER = Object.freeze({
  COMMENTARY: 10,
  AI_ANALYSIS: 20,
  SEARCH_LINES: 30,
  UCI_LOG: 40,
});

const UCI_LOG_MAX_LINES = 1000;
// Once the buffer overflows, trim this many lines in one go instead of
// one-per-incoming-line -- amortizes the layout cost at high info rates.
const UCI_LOG_TRIM_CHUNK = 100;
const HEADER_H = 44; // px -- approximate nav header height
const WIN_MARGIN = 8; // gap between window edge and WinBox

// Set by play.js on perspective mount/unmount.
let dockEl = null;
let dockResizeObs = null;
// Drag handles between adjacent docked slots. With N slots there are
// (N-1) grips; the array is rebuilt each time slots change via
// syncDockVisibility.
let dockGrips = [];
// Additional, independent dock containers (e.g. commentary). Each entry:
//   { el, resizeObs }
// These get the same bounds-tracking treatment as the debug dock but do
// NOT participate in slot/splitter accounting -- they are owned by their
// respective window factories via createDockableWindow's `getDockEl`.
const extraDocks = new Map();

// Per-slot flex-grow ratios, keyed by the instance's dockedKey. Survives
// slot additions/removals/reorders because each entry stands alone --
// missing entries fall back to DEFAULT_DOCK_GROW.
const DOCK_GROW_KEY = STORAGE_KEY.PLAY_DOCK_GROW;
const DEFAULT_DOCK_GROW = 1.0;

function loadDockGrows() {
  const parsed = loadJson(DOCK_GROW_KEY, {});
  return (parsed && typeof parsed === "object") ? parsed : {};
}

function saveDockGrows(grows) {
  saveJson(DOCK_GROW_KEY, grows);
}

// Mobile gate. Width OR short-height crosses into the stacked layout,
// matching the play perspective's CSS media query (see styles.css :root
// + breakpoints.js). Short-but-wide must count so JS stops driving the
// desktop fixed side-rail positioning the CSS no longer expects.
export function isMobileLayout() {
  return mqMobile.matches || mqMobileHPlay.matches;
}

function applyDockBounds(el) {
  if (!el || isMobileLayout()) return;
  const board = document.querySelector(".play-board-host");
  if (!board) return;
  const rect = board.getBoundingClientRect();
  const ribbonW = ribbonWidthPx(PLAY_GRID_SEL);
  const ribbonSide = document.body.dataset.ribbonSide === "right" ? "right" : "left";
  if (ribbonSide === "right") {
    el.style.width = (window.innerWidth - Math.round(rect.right) - ribbonW - 9) + "px";
  } else {
    el.style.width = (Math.round(rect.left) - ribbonW - 9) + "px";
  }

  const clockTop = document.querySelector(".clock-row.clock-top");
  const clockBot = document.querySelector(".clock-row.clock-bottom");
  if (clockTop && clockBot) {
    const top = Math.round(clockTop.getBoundingClientRect().top);
    const bot = Math.round(clockBot.getBoundingClientRect().bottom);
    el.style.top    = top + "px";
    el.style.bottom = (window.innerHeight - bot) + "px";
    el.style.height = "";
  }
}

function updateDockBounds() {
  applyDockBounds(dockEl);
  for (const { el } of extraDocks.values()) applyDockBounds(el);
}

// Board horizontal position shifts (e.g. left rail collapsing when the dock
// empties) don't trigger our ResizeObserver, which only fires on size change.
// Listen for layout-changed too; defer two frames so game-view's own rAF-driven
// recompute has settled the board's new left edge before we re-measure.
window.addEventListener(APP_EVT.LAYOUT_CHANGED, () => {
  requestAnimationFrame(() =>
    requestAnimationFrame(() => { updateDockBounds(); reclampFloats(); }));
});

// Re-clamp floating windows off the ribbon strips when the viewport shrinks
// (WinBox doesn't re-enforce left/right on resize). rAF-coalesced.
window.addEventListener("resize", rafCoalesce(reclampFloats));

// Migrate inline-capable panels across the mobile breakpoint. Each query can
// fire independently (width vs height), so a single coalesced handler covers
// both without double-running. relayout() no-ops for instances that aren't
// open or have no inline host.
function relayoutInlineInstances() {
  for (const inst of instances) {
    if (inst.relayout) inst.relayout();
  }
}
mqMobile.addEventListener("change", relayoutInlineInstances);
mqMobileHPlay.addEventListener("change", relayoutInlineInstances);

// Width that fits in the space to the right of the board, with fallback.
function rightColumnWidth(fallback = 480) {
  const board = document.querySelector(".play-board-host");
  if (!board) return fallback;
  const right = Math.round(board.getBoundingClientRect().right);
  const avail = window.innerWidth - right - WIN_MARGIN * 2;
  return Math.max(320, Math.min(avail, fallback));
}

// Strip reserved per viewport edge for the docked ribbon. Floating mode
// overlays content and reserves nothing; docked reserves only its own side
// so the opposite edge is fully usable. Side flips re-clamp existing floats.
function ribbonReserve() {
  if (document.body.dataset.ribbonFloat) return { left: 0, right: 0 };
  const w = ribbonWidthPx(PLAY_GRID_SEL);
  return document.body.dataset.ribbonSide === "right"
    ? { left: 0, right: w }
    : { left: w, right: 0 };
}

function winboxBase(title, className, width, height, x, y) {
  const { left, right } = ribbonReserve();
  return {
    title,
    class: `sturddle-wb ${className} no-full`,
    width,
    height,
    minwidth: 320,
    minheight: 120,
    x,
    y,
    top: HEADER_H,
    left,
    right,
  };
}

// Keep a floating window clear of the docked ribbon strip. WinBox enforces
// left/right only while dragging, not on a saved/initial position or a
// viewport/ribbon-side change, so re-clamp explicitly. Shrink an over-wide
// window to the usable band first, so its left edge can always reach `left`.
function clampFloatX(wb, { left, right }) {
  if (!wb) return;
  // Refresh WinBox's stored margins in every state: it clamps drag/move
  // against wb.left/wb.right and sizes maximized windows from them, and
  // these are otherwise frozen at creation time.
  wb.left = left;
  wb.right = right;
  const vw = window.innerWidth;
  const clampX = (x, w) => Math.max(left, Math.min(x, vw - right - w));
  if (wb.max) {
    // Re-fit a maximized window to the new band. Pass skip=true so the
    // stored restore geometry isn't clobbered.
    wb.resize(vw - left - right, window.innerHeight - wb.top - wb.bottom, true)
      .move(left, wb.top, true);
    return;
  }
  if (wb.min) {
    // A minimized bar's geometry lives in the DOM, not wb.x/wb.width; nudge
    // the rendered bar out of the strip without touching its restore size.
    const r = wb.g.getBoundingClientRect();
    const curX = Math.round(r.left);
    const x = clampX(curX, Math.round(r.width));
    if (x !== curX) wb.move(x, Math.round(r.top), true);
    return;
  }
  const band = vw - left - right;
  // Bail when the band can't fit the window's minwidth: shrinking below
  // it is worse than letting the window overlap a ribbon.
  if (band < (wb.minwidth || 1)) return;
  if (wb.width > band) wb.resize(band, wb.height);
  const x = clampX(wb.x, wb.width);
  if (x !== wb.x) wb.move(x, wb.y);
}

function reclampFloats() {
  const reserve = ribbonReserve();
  for (const inst of instances) clampFloatX(inst.wb, reserve);
}

// Geometry as raw numbers. Distinct from tournament-workspace.js's
// wbGeometryPx, which returns CSS px-strings -- not interchangeable.
function wbGeometryNum(wb) {
  return { x: wb.x, y: wb.y, width: wb.width, height: wb.height };
}

function loadGeo(key) {
  return loadJson(key);
}

function saveGeo(key, wb) {
  if (!wb) return;
  // Best-effort write (saveJson swallows quota/disabled-storage errors);
  // losing a geometry persist is harmless.
  saveJson(key, wbGeometryNum(wb));
}

function isDocked(key) {
  const v = loadRaw(key);
  return v === null ? true : v === "1"; // default: docked
}

function setDocked(key, val) {
  saveRaw(key, val ? "1" : "0");
}

function isOpen(key) {
  return loadRaw(key) === "1";
}

function setOpen(key, val) {
  saveRaw(key, val ? "1" : "0");
}

// -- dock container ----------------------------------------------------------

// Each docked window gets a .dock-slot child inside dockEl.
// slot structure:
//   .dock-slot
//     .dock-slot-header  (title + undock button)
//     .dock-slot-body    (the window's body div, transplanted here)

function syncExtraDocksVisibility() {
  for (const { el } of extraDocks.values()) {
    const wasEmpty = el.classList.contains("dock-empty");
    const isEmpty = el.querySelectorAll(".dock-slot").length === 0;
    el.classList.toggle("dock-empty", isEmpty);
    if (wasEmpty !== isEmpty) emitLayoutChanged();
  }
}

function emitLayoutChanged() {
  window.dispatchEvent(new CustomEvent(APP_EVT.LAYOUT_CHANGED));
}

function applyDockGrows() {
  if (!dockEl) return;
  const slots = dockEl.querySelectorAll(".dock-slot");
  // Single docked slot: force flex-grow=1 so it fills the whole dock
  // regardless of any stored ratio from a prior multi-slot session.
  if (slots.length <= 1) {
    for (const slot of slots) slot.style.flexGrow = "1";
    return;
  }
  const grows = loadDockGrows();
  for (const slot of slots) {
    const inst = instances.find(i => i.slot === slot);
    if (!inst) continue;
    const g = Number(grows[inst.dockedKey]);
    const value = Number.isFinite(g) && g > 0 ? g : DEFAULT_DOCK_GROW;
    slot.style.flexGrow = String(value);
  }
}

function persistGrow(key, value) {
  const grows = loadDockGrows();
  grows[key] = value;
  saveDockGrows(grows);
}

function attachGripDrag(grip, topInst, botInst) {
  grip.addEventListener("pointerdown", (eDown) => {
    if (eDown.button !== 0) return;
    eDown.preventDefault();
    try { grip.setPointerCapture(eDown.pointerId); } catch { /* */ }
    grip.classList.add("dragging");

    const topSlot = topInst.slot;
    const botSlot = botInst.slot;
    const topRect0 = topSlot.getBoundingClientRect();
    const botRect0 = botSlot.getBoundingClientRect();
    const pxRange = topRect0.height + botRect0.height;
    // Snapshot the flex-grow values so deltas are linear in pixels:
    // total grow units (= sum) maps to total pixels (= pxRange).
    const topG0 = parseFloat(getComputedStyle(topSlot).flexGrow) || DEFAULT_DOCK_GROW;
    const botG0 = parseFloat(getComputedStyle(botSlot).flexGrow) || DEFAULT_DOCK_GROW;
    const sumG = topG0 + botG0;
    const y0 = eDown.clientY;
    let pendingCollapse = null; // "top" | "bottom" | null

    const onMove = (e) => {
      const dy = e.clientY - y0;
      // Grip y movement maps 1:1 onto top-slot pixels; bottom absorbs
      // the inverse. Clamp to [0, pxRange] so flex-grow stays
      // non-negative; non-positive on either side flags collapse
      // intent for release. The grip itself can travel all the way to
      // either edge -- collapse fires when the slot would be zero.
      const topPx = Math.max(0, Math.min(topRect0.height + dy, pxRange));
      const botPx = pxRange - topPx;
      if (topPx <= 0) pendingCollapse = "top";
      else if (botPx <= 0) pendingCollapse = "bottom";
      else pendingCollapse = null;
      const topG = (topPx / pxRange) * sumG;
      const botG = (botPx / pxRange) * sumG;
      topSlot.style.flexGrow = String(topG);
      botSlot.style.flexGrow = String(botG);
    };

    const onUp = () => {
      grip.classList.remove("dragging");
      grip.removeEventListener("pointermove", onMove);
      grip.removeEventListener("pointerup", onUp);
      grip.removeEventListener("pointercancel", onUp);
      if (pendingCollapse === "top") { topInst.close(); return; }
      if (pendingCollapse === "bottom") { botInst.close(); return; }
      persistGrow(topInst.dockedKey, parseFloat(topSlot.style.flexGrow));
      persistGrow(botInst.dockedKey, parseFloat(botSlot.style.flexGrow));
    };

    grip.addEventListener("pointermove", onMove);
    grip.addEventListener("pointerup", onUp);
    grip.addEventListener("pointercancel", onUp);
  });
}

function clearDockGrips() {
  for (const g of dockGrips) g.remove();
  dockGrips = [];
}

function rebuildDockGrips() {
  clearDockGrips();
  if (!dockEl) return;
  const slots = Array.from(dockEl.querySelectorAll(".dock-slot"));
  for (let i = 0; i + 1 < slots.length; i++) {
    const topSlot = slots[i];
    const botSlot = slots[i + 1];
    const topInst = instances.find(inst => inst.slot === topSlot);
    const botInst = instances.find(inst => inst.slot === botSlot);
    if (!topInst || !botInst) continue;
    const grip = document.createElement("div");
    grip.className = "dock-grip";
    dockEl.insertBefore(grip, botSlot);
    attachGripDrag(grip, topInst, botInst);
    dockGrips.push(grip);
  }
}

function syncDockVisibility() {
  syncExtraDocksVisibility();
  if (!dockEl) return;
  const slots = dockEl.querySelectorAll(".dock-slot");
  const wasEmpty = dockEl.classList.contains("dock-empty");
  const isEmpty = slots.length === 0;
  dockEl.classList.toggle("dock-empty", isEmpty);
  if (wasEmpty !== isEmpty) emitLayoutChanged();
  applyDockGrows();
  rebuildDockGrips();
}

function makeDockSlot(title, bodyEl, onUndock, onClose, titleActions) {
  const slot = document.createElement("div");
  slot.className = "dock-slot";
  const closeBtnHtml = onClose
    ? `<button type="button" class="dock-slot-close" title="Close" aria-label="Close">
         <wa-icon name="xmark"></wa-icon>
       </button>` : "";
  slot.innerHTML = `
    <div class="dock-slot-header">
      <span class="dock-slot-title"></span>
      <span class="dock-slot-actions"></span>
      <button type="button" class="dock-slot-undock" title="Undock" aria-label="Undock">
        <wa-icon name="arrow-up-right-from-square"></wa-icon>
      </button>
      ${closeBtnHtml}
    </div>
    <div class="dock-slot-body"></div>
  `;
  slot.querySelector(".dock-slot-title").textContent = title;
  slot.querySelector(".dock-slot-undock").addEventListener("click", onUndock);
  if (onClose) slot.querySelector(".dock-slot-close").addEventListener("click", onClose);
  const actionsEl = slot.querySelector(".dock-slot-actions");
  for (const a of titleActions || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `dock-slot-action ${a.className || ""}`.trim();
    btn.title = a.title || "";
    btn.setAttribute("aria-label", a.title || "");
    btn.addEventListener("click", a.onClick);
    actionsEl.appendChild(btn);
  }
  slot.querySelector(".dock-slot-body").appendChild(bodyEl);
  return slot;
}

function detachBody(slot, body) {
  slot.querySelector(".dock-slot-body").removeChild(body);
}

function addDockButton(wb, onDock) {
  wb.addControl({ class: "wb-dock-ctrl", index: 0, click: onDock });
  wb.g.querySelector(".wb-dock-ctrl").title = "Dock";
}

// -- dockable window factory -------------------------------------------------

// Registry of instances so lifecycle helpers can iterate without naming them.
const instances = [];

// oversized-ok: single window-lifecycle state machine -- 12 closures
// (dock/undock/float/inline/relayout/close/...) over 8 shared placement flags
// (wb/slot/inlineSlot/body/saved/docking/navAway). The float<->dock<->inline
// transitions are mutually recursive and share every flag; splitting just
// relocates the state web behind a ctx object for no readability gain.
export function createDockableWindow(config) {
  const {
    title, className, geoKey, winStateKey, dockedKey, openKey,
    defaultW, defaultH, defaultY, build, dockOrder,
    getDockEl = () => dockEl,
    getInlineEl = null,
    onUserClose,
    closable = false,
    titleActions = [],
  } = config;

  let wb = null;
  let slot = null;
  let inlineSlot = null;
  let body = null;
  let off = null;
  let saved = null;
  let docking = false;  // float -> dock transition; onclose skips destroy
  let navAway = false;  // nav detach; onclose skips destroy
  let programmaticClose = false; // close() -> wb.close(); onclose skips onUserClose
  let currentTitle = title;

  function loadWinState() { return loadRaw(winStateKey); }
  function saveWinState(v) { saveRaw(winStateKey, v || null); }

  function setOff(fn) { off = fn; }

  function userClose() {
    close();
    if (onUserClose) onUserClose();
  }

  function dock() {
    const container = getDockEl();
    if (!container) return;
    if (wb) {
      saveGeo(geoKey, wb);
      wb.body.removeChild(body);
      docking = true;
      wb.close(); // onclose sees docking=true, skips destroy()
      docking = false;
    }
    setDocked(dockedKey, true);
    slot = makeDockSlot(currentTitle, body, undock, closable ? userClose : null, titleActions);
    // Insert in dockOrder ascending; lower order goes on top. The
    // `instances` array is in module-load order, not dockOrder, so we
    // must scan for the MIN-order sibling that's still higher than us
    // -- inserting before the first match in array order would put a
    // slot in the wrong place when higher-order siblings were created
    // first (e.g. UCI loads before AI).
    let anchor = null;
    for (const other of instances) {
      if (other === inst) continue;
      if (!other.slot || other.slot.parentElement !== container) continue;
      if (other.dockOrder <= dockOrder) continue;
      if (anchor === null || other.dockOrder < anchor.dockOrder) anchor = other;
    }
    if (anchor) container.insertBefore(slot, anchor.slot);
    else container.appendChild(slot);
    syncDockVisibility();
    applyDockBounds(container);
  }

  function undock() {
    if (!slot) return;
    detachBody(slot, body);
    slot.remove();
    slot = null;
    setDocked(dockedKey, false);
    syncDockVisibility();
    openFloat();
  }

  function openFloat() {
    const geo = saved ?? loadGeo(geoKey);
    const h = geo?.height ?? defaultH;
    const w = geo?.width ?? defaultW();
    const x = geo?.x ?? "right";
    const y = geo?.y ?? defaultY(h);
    saved = null;
    wb = new WinBox({
      ...winboxBase(currentTitle, className, w, h, x, y),
      mount: body,
      onclose() {
        if (wb) saveGeo(geoKey, wb);
        wb = null;
        if (docking || navAway) return; // body lives on
        // User-initiated close (WinBox X) when not flagged programmatic.
        // Persist the closed state so a hard refresh doesn't reopen.
        const userInitiated = !programmaticClose;
        if (userInitiated) inst.openedByAnalysis = false;
        setOpen(openKey, false);
        if (off) { off(); off = null; }
        body = null;
        if (userInitiated && onUserClose) onUserClose();
      },
      onminimize() { saveWinState("min"); },
      onmaximize() { saveWinState("max"); },
      onrestore()  { saveWinState(null); },
      onmove()     { saveGeo(geoKey, wb); },
      onresize()   { saveGeo(geoKey, wb); },
    });
    clampFloatX(wb, ribbonReserve());
    // WinBox addControl with index:0 PREPENDS into .wb-control, so the
    // LAST call ends up leftmost. Add dock first so it stays rightmost,
    // then actions in declaration order (each new one goes leftmost).
    addDockButton(wb, dock);
    for (const a of titleActions) {
      wb.addControl({ class: a.className, index: 0, click: a.onClick });
      // WinBox's addControl does not accept title/aria; set them
      // post-mount via querySelector. Caller must keep className unique
      // to avoid colliding with anything in body content.
      const outer = wb.body?.parentElement;
      const btn = outer?.querySelector(`.wb-control > .${a.className}`);
      if (btn) {
        btn.title = a.title || "";
        btn.setAttribute("aria-label", a.title || "");
      }
    }
    const ws = loadWinState();
    if (ws === "min") wb.minimize();
    else if (ws === "max") wb.maximize();
  }

  // Inline placement: a stacked host below the board on mobile, where the
  // dock column and floating WinBox are unavailable (dock is display:none,
  // float chrome is unusable on a phone). Reuses the dock-slot chrome inside
  // a <details open> so the panel folds; the host owns vertical sizing
  // (natural grow, page scrolls). Mutually exclusive with dock()/float.
  function inline() {
    const host = getInlineEl?.();
    if (!host) return;
    // Reuse dock-slot chrome (title + close). Undock is meaningless inline
    // (no column/float to pop to) and hidden via CSS. The header doubles as
    // a fold toggle: clicking it collapses the body, like a <details>.
    inlineSlot = makeDockSlot(currentTitle, body, undock, closable ? userClose : null, titleActions);
    inlineSlot.classList.add("inline-slot");
    const header = inlineSlot.querySelector(".dock-slot-header");
    header.addEventListener("click", (e) => {
      // Ignore clicks on the action buttons (close/undock/title actions).
      if (e.target.closest("button")) return;
      inlineSlot.classList.toggle("collapsed");
    });
    host.appendChild(inlineSlot);
    host.classList.remove("inline-empty");
  }

  function uninline() {
    if (!inlineSlot) return;
    const host = inlineSlot.parentElement;
    detachBody(inlineSlot, body);
    inlineSlot.remove();
    inlineSlot = null;
    if (host && host.querySelectorAll(".inline-slot").length === 0) {
      host.classList.add("inline-empty");
    }
  }

  function teardownSlot() {
    if (!slot && !inlineSlot) return;
    if (slot) {
      detachBody(slot, body);
      slot.remove();
      slot = null;
    }
    uninline();
    if (off) { off(); off = null; }
    body = null;
  }

  function detachSlotForNav() {
    if (slot) {
      detachBody(slot, body);
      slot.remove();
      slot = null;
    }
    uninline();
  }

  function close() {
    inst.openedByAnalysis = false;
    setOpen(openKey, false);
    if (wb) {
      programmaticClose = true;
      wb.close();
      programmaticClose = false;
      return;
    }
    teardownSlot();
    syncDockVisibility();
  }

  function toggle(events) {
    if (wb || slot || inlineSlot) { close(); return; }
    setOpen(openKey, true);
    if (!body) body = build(events, { setOff });
    if (isMobileLayout() && getInlineEl?.()) {
      inline();
    } else if (isDocked(dockedKey) && getDockEl()) {
      dock();
    } else {
      openFloat();
    }
  }

  function closeForNav() {
    if (wb) { saved = wbGeometryNum(wb); navAway = true; wb.close(); navAway = false; }
    detachSlotForNav();
  }

  function restore(events) {
    if (wb || slot || inlineSlot) return; // already open from a prior call
    if (body || saved || isOpen(openKey)) toggle(events);
  }

  // Migrate an open panel between inline (mobile) and dock/float (desktop)
  // when the viewport crosses the mobile breakpoint. Preserves body +
  // listeners; only the placement chrome is rebuilt.
  function relayout() {
    const open = wb || slot || inlineSlot;
    if (!open || !getInlineEl) return;
    const wantInline = isMobileLayout();
    // Guard the destination host BEFORE tearing down the current placement:
    // if the inline host is gone (e.g. mid-unmount), keep the float/dock
    // rather than orphaning the body with nowhere to land.
    if (wantInline && (wb || slot) && getInlineEl()) {
      if (wb) { saveGeo(geoKey, wb); docking = true; wb.body.removeChild(body); wb.close(); docking = false; }
      else { detachBody(slot, body); slot.remove(); slot = null; }
      inline();
      syncDockVisibility();
    } else if (!wantInline && inlineSlot) {
      uninline();
      if (isDocked(dockedKey) && getDockEl()) dock();
      else openFloat();
    }
  }

  function setTitle(next) {
    if (typeof next !== "string" || next === currentTitle) return;
    currentTitle = next;
    if (wb) wb.setTitle(next);
    const slotEl = slot || inlineSlot;
    if (slotEl) {
      const el = slotEl.querySelector(".dock-slot-title");
      if (el) el.textContent = next;
    }
  }

  const inst = {
    toggle, close, teardownSlot, closeForNav, restore, relayout, setTitle,
    get wb() { return wb; },
    get slot() { return slot; },
    get inlineSlot() { return inlineSlot; },
    get body() { return body; },
    dockedKey,
    dockOrder,
    usesMainDock: !config.getDockEl,
    // Set true when analysis opened this window (restoreViewAnalysisWindows);
    // the stop path closes only these, leaving user-opened windows alone.
    openedByAnalysis: false,
  };
  instances.push(inst);
  return inst;
}

export function setDockContainer(el) {
  if (dockResizeObs) { dockResizeObs.disconnect(); dockResizeObs = null; }
  window.removeEventListener("resize", updateDockBounds);
  // Defensive: tear down any leftover slots when detaching.
  // Only debug-window instances (default getDockEl -> module dockEl) are torn
  // down here; extra-dock owners (e.g. commentary) manage their own lifecycle.
  if (!el) {
    instances.forEach(i => { if (i.usesMainDock) i.teardownSlot(); });
    clearDockGrips();
  }
  dockEl = el;
  if (el) {
    const board = document.querySelector(".play-board-host");
    if (board) {
      dockResizeObs = new ResizeObserver(updateDockBounds);
      dockResizeObs.observe(board);
    }
    window.addEventListener("resize", updateDockBounds);
    updateDockBounds();
  }
  syncDockVisibility();
}

// Register an extra dock container so it gets the same bounds-tracking
// (resize observer + window resize listener) as the debug dock. Returns an
// unregister function. Independent of slot/splitter accounting.
export function registerExtraDock(el) {
  if (!el) return () => {};
  const board = document.querySelector(".play-board-host");
  const entry = { el, resizeObs: null };
  if (board) {
    entry.resizeObs = new ResizeObserver(updateDockBounds);
    entry.resizeObs.observe(board);
  }
  extraDocks.set(el, entry);
  window.addEventListener("resize", updateDockBounds);
  applyDockBounds(el);
  el.classList.toggle("dock-empty", el.querySelectorAll(".dock-slot").length === 0);
  return () => {
    const e = extraDocks.get(el);
    if (e?.resizeObs) e.resizeObs.disconnect();
    extraDocks.delete(el);
    if (!dockEl && extraDocks.size === 0) {
      window.removeEventListener("resize", updateDockBounds);
    }
  };
}

// -- UCI log body ------------------------------------------------------------

const UCI_GEO_KEY       = STORAGE_KEY.UCILOG_GEO;
const UCI_WIN_STATE_KEY = STORAGE_KEY.UCILOG_WIN_STATE;
const UCI_DOCKED_KEY    = STORAGE_KEY.UCILOG_DOCKED;
const UCI_OPEN_KEY      = STORAGE_KEY.UCILOG_OPEN;

function buildUciLogBody(events, { setOff }) {
  const body = document.createElement("div");
  body.className = "wb-uci-log";
  body.innerHTML = `
    <div class="wb-uci-log-toolbar">
      <label><input type="checkbox" class="uci-log-pause"> Pause</label>
      <button type="button" class="uci-log-copy" title="Copy to clipboard">Copy</button>
      <button type="button" class="uci-log-clear">Clear</button>
    </div>
    <div class="wb-uci-log-lines"></div>
  `;

  const lines = body.querySelector(".wb-uci-log-lines");
  const pauseChk = body.querySelector(".uci-log-pause");
  const copyBtn = body.querySelector(".uci-log-copy");
  const clearBtn = body.querySelector(".uci-log-clear");
  let lineCount = 0;
  let paused = false;

  copyBtn.disabled = true;
  pauseChk.addEventListener("change", () => { paused = pauseChk.checked; });
  copyBtn.addEventListener("click", () => {
    const text = Array.from(lines.children).map(d => d.textContent).join("\n");
    navigator.clipboard.writeText(text)
      .then(() => toast("UCI log copied to clipboard", { variant: "success", duration: 1500 }))
      .catch((e) => toast(`Copy failed: ${e.message}`, { variant: "danger" }));
  });
  clearBtn.addEventListener("click", () => { lines.textContent = ""; lineCount = 0; copyBtn.disabled = true; });

  // Autoscroll only when the user is already pinned to the bottom; otherwise
  // they're inspecting earlier output and new lines must not yank them away.
  setOff(events.on((evt) => {
    if (evt.kind !== "uci_log" || paused) return;
    const { dir, line } = evt.payload;
    // body.parentElement is wb.body when floating, .dock-slot-body when docked.
    const scroller = body.parentElement;
    const pinned = isPinnedToBottom(scroller, AUTOSCROLL_SLACK_LINE_PX);
    const div = document.createElement("div");
    div.className = `wb-uci-log-line ${dir === ">" ? "uci-out" : "uci-in"}`;
    div.textContent = `${dir} ${line}`;
    lines.appendChild(div);
    if (copyBtn.disabled) copyBtn.disabled = false;
    lineCount++;
    if (lineCount > UCI_LOG_MAX_LINES) {
      for (let i = 0; i < UCI_LOG_TRIM_CHUNK && lines.firstChild; i++) {
        lines.removeChild(lines.firstChild);
        lineCount--;
      }
    }
    if (pinned) scrollToBottom(scroller);
  }));

  return body;
}

const uciLog = createDockableWindow({
  title: "UCI Log",
  className: "sturddle-wb-uci-log",
  geoKey: UCI_GEO_KEY,
  winStateKey: UCI_WIN_STATE_KEY,
  dockedKey: UCI_DOCKED_KEY,
  openKey: UCI_OPEN_KEY,
  defaultW: () => rightColumnWidth(480),
  defaultH: 320,
  defaultY: (h) => {
    const clockBot = document.querySelector(".clock-row.clock-bottom");
    const botTop = clockBot ? Math.round(clockBot.getBoundingClientRect().top) : window.innerHeight;
    return botTop - h - WIN_MARGIN;
  },
  build: buildUciLogBody,
  dockOrder: DOCK_ORDER.UCI_LOG,
  closable: true,
});

// -- Search Lines body -------------------------------------------------------

const PV_GEO_KEY       = STORAGE_KEY.PVTABLE_GEO;
const PV_WIN_STATE_KEY = STORAGE_KEY.PVTABLE_WIN_STATE;
const PV_DOCKED_KEY    = STORAGE_KEY.PVTABLE_DOCKED;
const PV_OPEN_KEY      = STORAGE_KEY.PVTABLE_OPEN;

function buildPvTableBody(events, { setOff }) {
  const pvt = createPvTable({ colWidthsKey: STORAGE_KEY.PVTABLE_COL_WIDTHS });
  const off = events.on((evt) => {
    if (evt.kind !== KIND.ENGINE_INFO) return;
    pvt.update(evt.payload, evt.payload.pv?.[0]);
  });
  setOff(() => { off(); pvt.dispose(); });
  return pvt.el;
}

const pvTable = createDockableWindow({
  title: "Search Lines",
  className: "sturddle-wb-pvtable",
  geoKey: PV_GEO_KEY,
  winStateKey: PV_WIN_STATE_KEY,
  dockedKey: PV_DOCKED_KEY,
  openKey: PV_OPEN_KEY,
  defaultW: () => rightColumnWidth(560),
  defaultH: 260,
  defaultY: () => HEADER_H,
  build: buildPvTableBody,
  dockOrder: DOCK_ORDER.SEARCH_LINES,
  closable: true,
});

// -- public API --------------------------------------------------------------

const VIEW_UCI_OPEN_KEY = STORAGE_KEY.VIEW_UCILOG_OPEN;
const VIEW_PV_OPEN_KEY  = STORAGE_KEY.VIEW_PVTABLE_OPEN;

// User-driven toggles (ribbon buttons, analysis toast): a manual open
// transfers ownership, so the analysis stop path won't close the window.
// (restore() across nav reuses inst.toggle directly and must NOT clear.)
export function toggleUciLogWindow(events) { uciLog.openedByAnalysis = false; uciLog.toggle(events); }
export function togglePvTableWindow(events) { pvTable.openedByAnalysis = false; pvTable.toggle(events); }

export function closeDebugWindows() {
  instances.forEach(i => { if (i.usesMainDock) i.closeForNav(); });
  syncDockVisibility();
}

// Close only the windows analysis opened, leaving user-opened windows
// alone. The flag is set in restoreViewAnalysisWindows at open time, so
// there's nothing to reconstruct here.
export function closeAnalysisOpenedWindows() {
  instances.forEach(i => {
    if (i.usesMainDock && i.openedByAnalysis) i.close();
  });
  syncDockVisibility();
}

export function restoreDebugWindows(events) {
  instances.forEach(i => { if (i.usesMainDock) i.restore(events); });
}

// Save open state of debug windows as of the last view-mode analysis session.
export function snapshotViewAnalysisState() {
  setOpen(VIEW_UCI_OPEN_KEY, !!(uciLog.wb || uciLog.slot));
  setOpen(VIEW_PV_OPEN_KEY,  !!(pvTable.wb || pvTable.slot));
}

// Open debug windows based on the last view-mode analysis snapshot.
// Falls back to the shared open key on first use (before any snapshot exists).
export function restoreViewAnalysisWindows(events) {
  const uciShouldOpen = loadRaw(VIEW_UCI_OPEN_KEY) !== null
    ? isOpen(VIEW_UCI_OPEN_KEY) : isOpen(UCI_OPEN_KEY);
  const pvShouldOpen  = loadRaw(VIEW_PV_OPEN_KEY) !== null
    ? isOpen(VIEW_PV_OPEN_KEY)  : isOpen(PV_OPEN_KEY);
  if (uciShouldOpen && !uciLog.wb && !uciLog.slot) { uciLog.toggle(events); uciLog.openedByAnalysis = true; }
  if (pvShouldOpen  && !pvTable.wb && !pvTable.slot) { pvTable.toggle(events); pvTable.openedByAnalysis = true; }
}
