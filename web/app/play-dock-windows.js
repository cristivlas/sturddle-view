// Dockable windows for play mode (desktop only).
// 1. Search Lines: per-iteration principal variation, cutechess-style.
// 2. AI Analysis: streamed prose commentary from the AI agent.
// 3. UCI log: raw lines flowing between python-chess and the engine.
// 4. Engine Eval: the horizontal per-ply eval strip.
//
// Each window can float (WinBox) or dock into the left column of the play
// grid (.play-dock-left) -- or into the rail dock (.play-rail-dock), a
// capacity-one destination under the moves list where the eval strip lives
// by default. Which destination a window last docked into is persisted per
// window in DOCK_DEST_KEY. Dock state is persisted in localStorage; when
// two or more windows are docked, drag-grips between adjacent slots
// resize them (per-slot flex-grow ratios stored in DOCK_GROW_KEY).
// Besides the header buttons, windows dock/undock by drag: dropping a
// floating title bar on the dock column docks; dragging a slot header
// past a small threshold undocks into a float that follows the pointer.
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
import { createEvalBar, EVAL_EMPTY_CLASS } from "./eval-graph.js";
import { createPvTable } from "./pv-table.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadJson, saveJson, loadRaw, saveRaw } from "./storage.js";
import {
  AUTOSCROLL_SLACK_LINE_PX,
  headerBottomPx,
  isPinnedToBottom,
  markSelectable,
  rafCoalesce,
  ribbonWidthPx,
  scrollToBottom,
} from "./wb-utils.js";

const PLAY_GRID_SEL = ".play-grid";
const DOCK_SLOT_CLASS = "dock-slot";
const DOCK_GHOST_CLASS = "dock-ghost";
const DOCK_EMPTY_CLASS = "dock-empty";
const DOCK_DROP_ELIGIBLE_CLASS = "dock-drop-eligible";
const DOCK_SLOT_SEL = `.${DOCK_SLOT_CLASS}`;
const DOCK_GHOST_SEL = `.${DOCK_GHOST_CLASS}`;

// Vertical stack order for docked windows. Lower values render higher
// in the column. Centralized so adding a new window doesn't require
// guessing an unused number; the gaps between values leave room for
// future insertions without renumbering existing entries.
export const DOCK_ORDER = Object.freeze({
  COMMENTARY: 10,
  AI_ANALYSIS: 20,
  SEARCH_LINES: 30,
  UCI_LOG: 40,
  ENGINE_EVAL: 50,
});

const UCI_LOG_MAX_LINES = 1000;
// Once the buffer overflows, trim this many lines in one go instead of
// one-per-incoming-line -- amortizes the layout cost at high info rates.
const UCI_LOG_TRIM_CHUNK = 100;
const WIN_MARGIN = 8; // gap between window edge and WinBox
// Keep at least this much of a manually dragged window above the viewport
// bottom so its title bar stays reachable.
const FLOAT_DRAG_BOTTOM_MARGIN_PX = 44;
// Pointer travel before a slot-header drag undocks / a title-bar drag
// shows the dock drop hint; small enough to feel immediate, big enough
// that a sloppy click doesn't tear a window out.
const DRAG_DOCK_THRESHOLD_PX = 5;

// Set by play.js on perspective mount/unmount.
let dockEl = null;
// Rail dock: capacity-one destination under the moves list, positioned by
// game-view's positionSideRail. Also set by play.js on mount/unmount.
let railDockEl = null;
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

// Migrate panels across the mobile breakpoint: inline-capable ones swap
// placement, rail-docked ones evacuate the hidden rail. Each query can fire
// independently (width vs height), so a single coalesced handler covers both
// without double-running. relayout() no-ops for instances that aren't open.
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
    top: headerBottomPx(),
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

const EVAL_EMPTY_SEL = `.${EVAL_EMPTY_CLASS}`;
// Set on a slot whose occupant is an empty eval strip; CSS display:none's it.
const SLOT_HIDDEN_CLASS = "dock-slot-hidden";

// A slot holding an empty eval strip is hidden; count it out of dock-empty
// accounting so the container collapses/expands with the data.
function slotHidden(slot) {
  return !!slot.querySelector(EVAL_EMPTY_SEL);
}

// Stamp the hidden class on every live slot from its occupant's state.
// Driven from syncDockVisibility so all dock/undock/feed paths converge.
function syncHiddenSlots() {
  for (const inst of instances) {
    for (const el of [inst.slot, inst.inlineSlot]) {
      if (el) el.classList.toggle(SLOT_HIDDEN_CLASS, slotHidden(el));
    }
  }
}

// Toggle a container's dock-empty class from its live (visible) slot count,
// emitting LAYOUT_CHANGED on transition. Shared by all dock containers.
function syncEmptyClass(el) {
  const wasEmpty = el.classList.contains(DOCK_EMPTY_CLASS);
  const isEmpty = Array.from(el.querySelectorAll(DOCK_SLOT_SEL)).every(slotHidden);
  el.classList.toggle(DOCK_EMPTY_CLASS, isEmpty);
  if (wasEmpty !== isEmpty) emitLayoutChanged();
}

function syncExtraDocksVisibility() {
  for (const { el } of extraDocks.values()) syncEmptyClass(el);
}

function emitLayoutChanged() {
  window.dispatchEvent(new CustomEvent(APP_EVT.LAYOUT_CHANGED));
}

// Stored flex-grow for a dockedKey, falling back to the default. `grows` is a
// once-parsed store; passing it avoids re-reading localStorage per slot.
function growFor(grows, key) {
  const g = Number(grows[key]);
  return Number.isFinite(g) && g > 0 ? g : DEFAULT_DOCK_GROW;
}

// Main-dock slots that participate in the flex-grow/grip ratio system:
// all visible ones (empty-eval-hidden slots are counted out).
function growSlots() {
  if (!dockEl) return [];
  return Array.from(dockEl.querySelectorAll(DOCK_SLOT_SEL)).filter(s => !slotHidden(s));
}

// Size the dock's growing flex children. `extraKey` (the drop-ghost's
// dockedKey, when previewing) is counted as a virtual slot so the preview
// split matches the post-dock split exactly. A lone slot OR lone ghost
// fills the whole dock, ignoring any stored ratio from a prior multi-slot
// session. Hidden slots are left alone.
function applyDockGrows(extraKey = null) {
  if (!dockEl) return;
  const slots = growSlots();
  const ghost = dockEl.querySelector(DOCK_GHOST_SEL);
  const ghostCounts = !!ghost && !!extraKey;
  if (slots.length + (ghostCounts ? 1 : 0) <= 1) {
    for (const slot of slots) slot.style.flexGrow = "1";
    if (ghostCounts) ghost.style.flexGrow = "1";
    return;
  }
  const grows = loadDockGrows();
  for (const slot of slots) {
    const inst = instances.find(i => i.slot === slot);
    if (inst) slot.style.flexGrow = String(growFor(grows, inst.dockedKey));
  }
  if (ghostCounts) ghost.style.flexGrow = String(growFor(grows, extraKey));
}

function persistGrow(key, value) {
  const grows = loadDockGrows();
  grows[key] = value;
  saveDockGrows(grows);
}

// Which dock a main-dock window lands in when docking: the shared left
// column or the rail slot. Keyed by dockedKey, like DOCK_GROW_KEY.
const DOCK_DEST_KEY = STORAGE_KEY.PLAY_DOCK_DEST;
const DOCK_DEST_MAIN = "main";
const DOCK_DEST_RAIL = "rail";

function loadDockDests() {
  const parsed = loadJson(DOCK_DEST_KEY, {});
  return (parsed && typeof parsed === "object") ? parsed : {};
}

function destFor(key, fallback) {
  const d = loadDockDests()[key];
  return d === DOCK_DEST_MAIN || d === DOCK_DEST_RAIL ? d : fallback;
}

function setDest(key, val) {
  const dests = loadDockDests();
  dests[key] = val;
  saveJson(DOCK_DEST_KEY, dests);
}

// The rail dock holds at most one visible slot; it's free for `inst` when
// empty or when the occupant is inst's own slot (re-dock while dragging
// out). A hidden (empty-eval) squatter yields too: dock() evicts it into
// the main dock -- so it only counts as free when there is one to evict to.
function railFreeFor(inst) {
  // Mobile hides the rail (CSS), so nothing may land there. Gated here --
  // the single chokepoint every rail consumer routes through -- rather than
  // repeated at each call site, where one omission strands a window in a
  // display:none container.
  if (!railDockEl || isMobileLayout()) return false;
  const occupant = railDockEl.querySelector(DOCK_SLOT_SEL);
  if (!occupant || occupant === inst.slot) return true;
  return !!dockEl && slotHidden(occupant);
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

    // Collapse closes the window -- except non-closable ones (nothing could
    // revive them), which pop out to a float instead.
    const collapse = (inst) => { if (inst.closable) inst.close(); else inst.undock(); };
    const onUp = () => {
      grip.classList.remove("dragging");
      grip.removeEventListener("pointermove", onMove);
      grip.removeEventListener("pointerup", onUp);
      grip.removeEventListener("pointercancel", onUp);
      if (pendingCollapse === "top") { collapse(topInst); return; }
      if (pendingCollapse === "bottom") { collapse(botInst); return; }
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
  // Only visible slots get grips: a grip against a hidden neighbor would
  // be a stray handle resizing nothing.
  const slots = growSlots();
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
  syncHiddenSlots();
  syncExtraDocksVisibility();
  if (railDockEl) syncEmptyClass(railDockEl);
  if (!dockEl) return;
  syncEmptyClass(dockEl);
  applyDockGrows();
  rebuildDockGrips();
}

function pointerOverEl(el, e) {
  const r = el.getBoundingClientRect();
  return e.clientX >= r.left && e.clientX <= r.right &&
         e.clientY >= r.top && e.clientY <= r.bottom;
}

// The sibling slot that `inst` docks before, or null to append last. Shared
// by dock() and the drop-ghost so the preview lands exactly where dock() puts
// it: the MIN-dockOrder slot in this container still ranked below inst.
function findDockAnchor(container, inst) {
  let anchor = null;
  for (const other of instances) {
    if (other === inst) continue;
    if (!other.slot || other.slot.parentElement !== container) continue;
    if (other.dockOrder <= inst.dockOrder) continue;
    if (anchor === null || other.dockOrder < anchor.dockOrder) anchor = other;
  }
  return anchor;
}

// Place `el` (a real slot or the drop-ghost) at inst's dockOrder boundary.
function insertInDockOrder(container, el, inst) {
  const anchor = findDockAnchor(container, inst);
  if (anchor) container.insertBefore(el, anchor.slot);
  else container.appendChild(el);
}

// Preview where a dragged window will dock: a ghost flex child inserted at
// inst's landing boundary with its would-be flex-grow, so the browser reflows
// existing slots to their true post-dock heights. Idempotent; the ghost is a
// real flex sibling, not an overlay, so the split matches dock() exactly.
function showDockGhost(container, inst) {
  if (!container) return;
  let ghost = container.querySelector(DOCK_GHOST_SEL);
  if (!ghost) {
    ghost = document.createElement("div");
    ghost.className = DOCK_GHOST_CLASS;
  }
  insertInDockOrder(container, ghost, inst);
  // Size existing slots + ghost with the same pass dock() uses, so the
  // preview split equals the post-dock split (the lone-slot flex:1 override
  // no longer applies once the ghost is a second child). The rail dock holds
  // a single slot, so its ghost just fills it (CSS flex:1) -- no grow pass.
  if (container === dockEl) applyDockGrows(inst.dockedKey);
}

function hideDockGhost(container) {
  const ghost = container?.querySelector(DOCK_GHOST_SEL);
  if (!ghost) return;
  ghost.remove();
  // Restore real-slot sizing: a now-lone slot must snap back to flex:1,
  // which the ghost's virtual-slot pass had suppressed.
  if (container === dockEl) applyDockGrows();
}

// Track a pointer drag against a set of dock containers. Past
// DRAG_DOCK_THRESHOLD_PX every container shows its drop hint (visible even
// when dock-empty), hovering one toggles its active state, and onEnd reports
// the container the pointer was released over (or null). When ghostInst is
// given, a landing preview is shown at its dock boundary in the hovered
// container. Listeners go on document so they survive the originating
// element being detached mid-drag (slot removal on undock).
function watchDockDrop(containers, eDown, { onFirstMove, onMove, onEnd, ghostInst }) {
  const x0 = eDown.clientX, y0 = eDown.clientY;
  let started = false, over = null;
  const move = (e) => {
    if (!started) {
      if (Math.hypot(e.clientX - x0, e.clientY - y0) < DRAG_DOCK_THRESHOLD_PX) return;
      started = true;
      for (const c of containers) c.classList.add(DOCK_DROP_ELIGIBLE_CLASS);
      if (onFirstMove) onFirstMove(e);
    }
    over = containers.find(c => pointerOverEl(c, e)) ?? null;
    if (ghostInst) {
      for (const c of containers) {
        if (c === over) showDockGhost(c, ghostInst);
        else hideDockGhost(c);
      }
    }
    if (onMove) onMove(e);
  };
  const finish = (drop) => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    document.removeEventListener("pointercancel", cancel);
    for (const c of containers) {
      c.classList.remove(DOCK_DROP_ELIGIBLE_CLASS);
      hideDockGhost(c);
    }
    if (onEnd) onEnd(drop && started ? over : null);
  };
  const up = () => finish(true);
  const cancel = () => finish(false);
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
  document.addEventListener("pointercancel", cancel);
}

function makeDockSlot(title, bodyEl, onUndock, onClose, titleActions) {
  const slot = document.createElement("div");
  slot.className = DOCK_SLOT_CLASS;
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
    defaultDest = DOCK_DEST_MAIN,
    railDockable = false,
  } = config;
  const mainDock = !config.getDockEl;

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

  // A non-closable window is always open, so its openKey must not exist:
  // clear any stale entry persisted back when the window was closable, and
  // refuse writes -- persisting "closed" for one is a bug, not a state.
  if (!closable) saveRaw(openKey, null);

  function persistOpen(v) {
    if (!closable) {
      if (!v) throw new Error(`${title}: cannot persist closed on a non-closable window`);
      return; // open is implied; keep the key absent
    }
    setOpen(openKey, v);
  }

  function openState() {
    return !closable || isOpen(openKey);
  }

  // Container this window docks into absent an explicit drop target: the
  // rail slot when that's its persisted destination and the slot is free,
  // else its own dock (main column or extra dock).
  function resolveDockEl() {
    if (railDockable && destFor(dockedKey, defaultDest) === DOCK_DEST_RAIL
        && railFreeFor(inst)) {
      return railDockEl;
    }
    return getDockEl();
  }

  // Every container this window may be drag-docked into.
  function dropTargets() {
    const targets = [];
    const own = getDockEl();
    if (own) targets.push(own);
    if (railDockable && railFreeFor(inst)) targets.push(railDockEl);
    return targets;
  }

  function setOff(fn) { off = fn; }

  function userClose() {
    close();
    if (onUserClose) onUserClose();
  }

  // Dragging a slot header past the threshold undocks into a float that
  // then follows the pointer manually (WinBox's own drag never started, so
  // we own this one). Dropping back on the dock re-docks. Buttons in the
  // header keep their click behavior via the closest() guard.
  function attachSlotDragUndock(slotEl) {
    const header = slotEl.querySelector(".dock-slot-header");
    header.addEventListener("pointerdown", (eDown) => {
      if (eDown.button !== 0 || eDown.target.closest("button")) return;
      if (isMobileLayout()) return;
      const targets = dropTargets();
      if (!targets.length) return;
      eDown.preventDefault(); // no text selection while dragging
      // Grab offsets: the pointer stays on the same spot of the header
      // once the slot becomes a floating window.
      const rect0 = slotEl.getBoundingClientRect();
      const grabX = eDown.clientX - Math.round(rect0.left);
      const grabY = eDown.clientY - Math.round(rect0.top);
      const moveFloatTo = (e) => {
        if (!wb) return;
        const vw = window.innerWidth;
        const x = Math.max(wb.left,
          Math.min(e.clientX - grabX, vw - wb.right - wb.width));
        const y = Math.max(wb.top,
          Math.min(e.clientY - grabY,
                   window.innerHeight - FLOAT_DRAG_BOTTOM_MARGIN_PX));
        wb.move(x, y);
      };
      watchDockDrop(targets, eDown, {
        onFirstMove(e) {
          // Tear-off: float at the slot's own position/size. (The header
          // undock button keeps restoring the last floating geometry.)
          saved = {
            x: Math.round(rect0.left), y: Math.round(rect0.top),
            width: Math.round(rect0.width), height: Math.round(rect0.height),
          };
          undock();
          // openFloat restores a persisted min/max state; force normal so
          // the window is actually draggable under the pointer.
          if (wb && (wb.min || wb.max)) wb.restore();
          moveFloatTo(e);
        },
        onMove: moveFloatTo,
        onEnd(overDock) { if (overDock && wb) dock(overDock, { persistDest: true }); },
        ghostInst: inst,
      });
    });
  }

  // Dropping a floating window's title bar on the dock column docks it.
  // WinBox's own drag keeps moving the window; we only watch the pointer.
  // Its drag-end handler is safe to run after dock() closed the window
  // (it only clears the wb-lock class and its own listeners).
  function attachFloatDragDock() {
    const dragEl = wb.g.querySelector(".wb-drag");
    if (!dragEl) return;
    dragEl.addEventListener("pointerdown", (eDown) => {
      if (eDown.button !== 0) return;
      const targets = dropTargets();
      if (!targets.length || isMobileLayout() || wb.min || wb.max) return;
      watchDockDrop(targets, eDown, {
        onEnd(overDock) { if (overDock && wb && !wb.max) dock(overDock, { persistDest: true }); },
        ghostInst: inst,
      });
    });
  }

  // persistDest: only a deliberate placement (drag-drop, dock button) may
  // rewrite the remembered destination. Fallback docks -- reopen, uninline,
  // mobile evacuation -- must land somewhere without erasing where the user
  // last put this window.
  function dock(toContainer = null, { persistDest = false } = {}) {
    const container = toContainer ?? resolveDockEl();
    if (!container) return;
    if (container === railDockEl) {
      // Only a hidden (empty-eval) squatter can be here -- railFreeFor
      // gates out visible occupants -- and it yields to the main dock.
      const occupant = instances.find(i =>
        i !== inst && i.slot && i.slot.parentElement === railDockEl);
      if (occupant && dockEl) occupant.redock(dockEl);
    }
    if (wb) {
      saveGeo(geoKey, wb);
      wb.body.removeChild(body);
      docking = true;
      wb.close(); // onclose sees docking=true, skips destroy()
      docking = false;
    }
    setDocked(dockedKey, true);
    if (persistDest && railDockable && railDockEl) {
      setDest(dockedKey, container === railDockEl ? DOCK_DEST_RAIL : DOCK_DEST_MAIN);
    }
    slot = makeDockSlot(currentTitle, body, undock, closable ? userClose : null, titleActions);
    attachSlotDragUndock(slot);
    hideDockGhost(container);
    insertInDockOrder(container, slot, inst);
    syncDockVisibility();
    // The rail's geometry is owned by game-view's positionSideRail; writing
    // left-column bounds onto it would flash it at the board's left edge.
    if (container !== railDockEl) applyDockBounds(container);
  }

  // Detach the docked slot chrome, keeping the body alive for its next home.
  function dropSlot() {
    if (!slot) return;
    detachBody(slot, body);
    slot.remove();
    slot = null;
  }

  function undock() {
    if (!slot) return;
    dropSlot();
    setDocked(dockedKey, false);
    syncDockVisibility();
    openFloat();
  }

  // Relocate a docked slot into another container (rail eviction).
  function redock(container) {
    if (!slot) return;
    dropSlot();
    dock(container);
  }

  function openFloat() {
    const geo = saved ?? loadGeo(geoKey);
    const h = geo?.height ?? defaultH;
    const w = geo?.width ?? defaultW();
    const x = geo?.x ?? "right";
    const y = geo?.y ?? defaultY(h);
    saved = null;
    wb = new WinBox({
      // no-close: WinBox's native hide-the-X modifier; a non-closable
      // window would otherwise be unrevivable once its float is closed.
      ...winboxBase(currentTitle, closable ? className : `${className} no-close`, w, h, x, y),
      mount: body,
      onclose() {
        if (wb) saveGeo(geoKey, wb);
        wb = null;
        if (docking || navAway) return; // body lives on
        // User-initiated close (WinBox X) when not flagged programmatic.
        // Persist the closed state so a hard refresh doesn't reopen.
        const userInitiated = !programmaticClose;
        if (userInitiated) inst.openedByAnalysis = false;
        persistOpen(false);
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
    // WinBox clamps drags against `top` but takes a saved/initial y as-is;
    // re-clamp so a stale geometry can't sit above the header line.
    if (wb.y < wb.top) wb.move(wb.x, wb.top);
    attachFloatDragDock();
    // WinBox addControl with index:0 PREPENDS into .wb-control, so the
    // LAST call ends up leftmost. Add dock first so it stays rightmost,
    // then actions in declaration order (each new one goes leftmost).
    // Wrap dock: the click handler's event arg must not become toContainer.
    addDockButton(wb, () => dock(null, { persistDest: true }));
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
    dropSlot();
    uninline();
    if (off) { off(); off = null; }
    body = null;
  }

  function detachSlotForNav() {
    dropSlot();
    uninline();
  }

  function close() {
    inst.openedByAnalysis = false;
    persistOpen(false);
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
    persistOpen(true);
    if (!body) body = build(events, { setOff });
    if (isMobileLayout() && getInlineEl?.()) {
      inline();
    } else if (isDocked(dockedKey) && resolveDockEl()) {
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
    if (body || saved || openState()) toggle(events);
  }

  // Migrate an open panel between inline (mobile) and dock/float (desktop)
  // when the viewport crosses the mobile breakpoint. Preserves body +
  // listeners; only the placement chrome is rebuilt.
  function relayout() {
    const open = wb || slot || inlineSlot;
    if (!open) return;
    // Rail-dockable windows migrate on the breakpoint without an inline host:
    // into mobile the rail is hidden, so evacuate to the main dock; back on
    // desktop, return to the rail if that is still the remembered home and it
    // is free. The saved dest survives the round trip (dock persists it only
    // on deliberate placement), so this restores the user's own choice.
    if (railDockable && slot) {
      const inRail = slot.parentElement === railDockEl;
      if (inRail && isMobileLayout()) {
        dropSlot();
        if (getDockEl()) dock(getDockEl());
        else openFloat();
        syncDockVisibility();
        return;
      }
      // resolveDockEl owns "does this window belong in the rail" -- asking it
      // keeps the answer identical to what a reopen would pick. It already
      // gates on mobile via railFreeFor, so no separate check here.
      if (!inRail && resolveDockEl() === railDockEl) {
        dropSlot();
        dock(railDockEl);
        syncDockVisibility();
        return;
      }
    }
    if (!getInlineEl) return;
    const wantInline = isMobileLayout();
    // Guard the destination host BEFORE tearing down the current placement:
    // if the inline host is gone (e.g. mid-unmount), keep the float/dock
    // rather than orphaning the body with nowhere to land.
    if (wantInline && (wb || slot) && getInlineEl()) {
      if (wb) { saveGeo(geoKey, wb); docking = true; wb.body.removeChild(body); wb.close(); docking = false; }
      else dropSlot();
      inline();
      syncDockVisibility();
    } else if (!wantInline && inlineSlot) {
      uninline();
      if (isDocked(dockedKey) && resolveDockEl()) dock();
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
    toggle, close, undock, redock, teardownSlot, closeForNav, restore, relayout, setTitle,
    get wb() { return wb; },
    get slot() { return slot; },
    get inlineSlot() { return inlineSlot; },
    get body() { return body; },
    dockedKey,
    dockOrder,
    closable,
    usesMainDock: mainDock,
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

// Rail dock container under the moves list (positioned by game-view's
// positionSideRail, so no bounds tracking here). Pass null on unmount --
// after closeDebugWindows/setDockContainer(null), which tear down any slot
// still parked in it.
export function setRailDockContainer(el) {
  railDockEl = el;
  if (el) syncEmptyClass(el);
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
  syncEmptyClass(el);
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

const UCI_LOG_TITLE     = "UCI Log";
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
  markSelectable(lines);
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
  title: UCI_LOG_TITLE,
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
  railDockable: true,
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
  defaultY: () => headerBottomPx(),
  build: buildPvTableBody,
  dockOrder: DOCK_ORDER.SEARCH_LINES,
  closable: true,
  railDockable: true,
});

// -- Engine Eval body --------------------------------------------------------

const EVAL_TITLE = "Engine Eval";
const EVAL_TOOLTIP = "Evaluation from the engine's point of view";
const EVAL_GEO_KEY       = STORAGE_KEY.EVALBAR_GEO;
const EVAL_WIN_STATE_KEY = STORAGE_KEY.EVALBAR_WIN_STATE;
const EVAL_DOCKED_KEY    = STORAGE_KEY.EVALBAR_DOCKED;
const EVAL_OPEN_KEY      = STORAGE_KEY.EVALBAR_OPEN;

// Bar click/navigability handlers close over per-mount perspective state,
// but the canvas (and its listeners) lives as long as the window body --
// across remounts. Handlers read through this indirection so they never
// capture a stale mount's closures; play.js swaps them on mount/unmount.
let evalCallbacks = null;
export function setEvalBarCallbacks(cb) { evalCallbacks = cb; }

// Live eval-strip widget ({setSamples, clear}) or null while the window is
// closed. play.js feeds it from board_update's eval_history.
let evalBarApi = null;
export function getEvalBarApi() { return evalBarApi; }

function buildEvalBarBody(_events, { setOff }) {
  const bar = createEvalBar({
    onBarClick: (ply) => evalCallbacks?.onBarClick(ply),
    isBarNavigable: (ply) => !!evalCallbacks?.isBarNavigable(ply),
  });
  // Base tooltip for empty regions; per-bar hover overrides it on the canvas.
  bar.el.title = EVAL_TOOLTIP;
  bar.setVisible(true);
  // Feeding can flip the strip's empty state, which hides/shows this
  // window's slot. Re-run dock accounting (dock-empty class, grips,
  // board-rail shrink) only on an actual flip: feeds arrive per
  // board_update, and rebuilding grips would kill an in-progress grip drag.
  const syncIfFlipped = (mutate) => {
    const wasEmpty = bar.el.classList.contains(EVAL_EMPTY_CLASS);
    mutate();
    if (bar.el.classList.contains(EVAL_EMPTY_CLASS) !== wasEmpty) syncDockVisibility();
  };
  evalBarApi = {
    setSamples: (items, engineIsWhite) => syncIfFlipped(() => bar.setSamples(items, engineIsWhite)),
    clear: () => syncIfFlipped(() => bar.clear()),
  };
  setOff(() => { evalBarApi = null; bar.dispose(); });
  return bar.el;
}

createDockableWindow({
  title: EVAL_TITLE,
  className: "sturddle-wb-evalbar",
  geoKey: EVAL_GEO_KEY,
  winStateKey: EVAL_WIN_STATE_KEY,
  dockedKey: EVAL_DOCKED_KEY,
  openKey: EVAL_OPEN_KEY,
  defaultW: () => rightColumnWidth(480),
  defaultH: 140,
  defaultY: (h) => {
    const clockBot = document.querySelector(".clock-row.clock-bottom");
    const botTop = clockBot ? Math.round(clockBot.getBoundingClientRect().top) : window.innerHeight;
    return botTop - h - WIN_MARGIN;
  },
  build: buildEvalBarBody,
  dockOrder: DOCK_ORDER.ENGINE_EVAL,
  // Not closable: nothing in the UI reopens it, so it must stay revivable
  // -- always open, movable between rail/dock/float.
  closable: false,
  defaultDest: DOCK_DEST_RAIL,
  railDockable: true,
});

// -- public API --------------------------------------------------------------

const VIEW_UCI_OPEN_KEY = STORAGE_KEY.VIEW_UCILOG_OPEN;
const VIEW_PV_OPEN_KEY  = STORAGE_KEY.VIEW_PVTABLE_OPEN;

// User-driven toggles (ribbon buttons, analysis toast): a manual open
// transfers ownership, so the analysis stop path won't close the window.
// (restore() across nav reuses inst.toggle directly and must NOT clear.)
export function toggleUciLogWindow(events) { uciLog.openedByAnalysis = false; uciLog.toggle(events); }
export function togglePvTableWindow(events) { pvTable.openedByAnalysis = false; pvTable.toggle(events); }

// Name the engine whose UCI traffic the log is showing; CSS ellipsis-truncates.
// Caller resolves the right engine (analysis engine while analyzing, play engine
// otherwise). Empty/falsy reverts to the bare title.
export function setUciLogEngine(name) {
  uciLog.setTitle(name ? `${UCI_LOG_TITLE} (${name})` : UCI_LOG_TITLE);
}

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
