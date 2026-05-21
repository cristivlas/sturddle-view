// Dockable windows for play mode (desktop only).
// 1. UCI log: raw lines flowing between python-chess and the engine.
// 2. Search Lines: per-iteration principal variation, cutechess-style.
//
// Each window can float (WinBox) or dock into the left column of the play
// grid (.play-dock-left, aka the "UCI dock"). Dock state is persisted in
// localStorage.
//
// The exported createDockableWindow factory is reused by play-commentary-
// window.js, which supplies its own dock container (.play-comments-host)
// via getDockEl. Such instances are flagged !sharesUciDock so debug-dock
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

import { attachColumnResize } from "./col-resize.js";
import { toast } from "./dialogs.js";
import { makeSplitter } from "./splitter.js";
import { mqMobile } from "./breakpoints.js";

const UCI_LOG_MAX_LINES = 1000;
// Once the buffer overflows, trim this many lines in one go instead of
// one-per-incoming-line -- amortizes the layout cost at high info rates.
const UCI_LOG_TRIM_CHUNK = 100;
const HEADER_H = 44; // px -- approximate nav header height
const WIN_MARGIN = 8; // gap between window edge and WinBox

// Set by play.js on perspective mount/unmount.
let dockEl = null;
let dockResizeObs = null;
let dockGrip = null;
// Additional, independent dock containers (e.g. commentary). Each entry:
//   { el, resizeObs }
// These get the same bounds-tracking treatment as the debug dock but do
// NOT participate in slot/splitter accounting -- they are owned by their
// respective window factories via createDockableWindow's `getDockEl`.
const extraDocks = new Map();

const DOCK_SPLIT_KEY = "sturddle:play:dockSplit";

// Mobile gate. Sourced from the shared --bp-mobile CSS custom property so
// the breakpoint lives in one place (see styles.css :root + breakpoints.js).
export function isMobileLayout() {
  return mqMobile.matches;
}

function applyDockBounds(el) {
  if (!el || isMobileLayout()) return;
  const board = document.querySelector(".play-board-host");
  if (!board) return;
  const rect = board.getBoundingClientRect();
  const ribbonW = parseInt(getComputedStyle(el.closest(".play-grid") ?? document.documentElement)
    .getPropertyValue("--ribbon-w")) || 36;
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
window.addEventListener("sturddle:layout-changed", () => {
  requestAnimationFrame(() =>
    requestAnimationFrame(updateDockBounds));
});

// Width that fits in the space to the right of the board, with fallback.
function rightColumnWidth(fallback = 480) {
  const board = document.querySelector(".play-board-host");
  if (!board) return fallback;
  const right = Math.round(board.getBoundingClientRect().right);
  const avail = window.innerWidth - right - WIN_MARGIN * 2;
  return Math.max(320, Math.min(avail, fallback));
}

function winboxBase(title, className, width, height, x, y) {
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
  };
}

function wbGeometry(wb) {
  return { x: wb.x, y: wb.y, width: wb.width, height: wb.height };
}

function loadGeo(key) {
  try { return JSON.parse(localStorage.getItem(key)) || null; } catch { return null; }
}

function saveGeo(key, wb) {
  if (!wb) return;
  localStorage.setItem(key, JSON.stringify(wbGeometry(wb)));
}

function isDocked(key) {
  const v = localStorage.getItem(key);
  return v === null ? true : v === "1"; // default: docked
}

function setDocked(key, val) {
  localStorage.setItem(key, val ? "1" : "0");
}

function isOpen(key) {
  return localStorage.getItem(key) === "1";
}

function setOpen(key, val) {
  localStorage.setItem(key, val ? "1" : "0");
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
  window.dispatchEvent(new CustomEvent("sturddle:layout-changed"));
}

function syncDockVisibility() {
  syncExtraDocksVisibility();
  if (!dockEl) return;
  const slots = dockEl.querySelectorAll(".dock-slot");
  const wasEmpty = dockEl.classList.contains("dock-empty");
  const isEmpty = slots.length === 0;
  dockEl.classList.toggle("dock-empty", isEmpty);
  if (wasEmpty !== isEmpty) emitLayoutChanged();
  const bothDocked = slots.length === 2;
  dockEl.classList.toggle("dock-single", !bothDocked);
  if (bothDocked && !dockGrip) {
    dockGrip = document.createElement("div");
    dockGrip.className = "dock-grip";
    // Insert between the two slots (after first, before second).
    const [first, second] = slots;
    dockEl.insertBefore(dockGrip, second);
    // slots[0] = top (lower dockOrder), slots[1] = bottom (higher dockOrder).
    const [topSlot, botSlot] = slots;
    const topInst = instances.find(i => i.slot === topSlot);
    const botInst = instances.find(i => i.slot === botSlot);
    makeSplitter({
      handle: dockGrip,
      container: dockEl,
      orientation: "vertical",
      cssVar: "--dock-split-ratio",
      storageKey: DOCK_SPLIT_KEY,
      defaultRatio: 0.5,
      onCollapse(side) {
        if (side === "before" && topInst) topInst.close();
        else if (side === "after" && botInst) botInst.close();
      },
    });
  } else if (!bothDocked && dockGrip) {
    dockGrip.remove();
    dockGrip = null;
  }
}

function makeDockSlot(title, bodyEl, onUndock, onClose) {
  const slot = document.createElement("div");
  slot.className = "dock-slot";
  const closeBtnHtml = onClose
    ? `<button type="button" class="dock-slot-close" title="Close" aria-label="Close">
         <wa-icon name="xmark"></wa-icon>
       </button>` : "";
  slot.innerHTML = `
    <div class="dock-slot-header">
      <span class="dock-slot-title"></span>
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
  slot.querySelector(".dock-slot-body").appendChild(bodyEl);
  return slot;
}

function detachBody(slot, body) {
  slot.querySelector(".dock-slot-body").removeChild(body);
}

function addDockButton(wb, onDock) {
  wb.addControl({ class: "wb-dock-ctrl", index: 0, click: onDock });
}

// -- dockable window factory -------------------------------------------------

// Registry of instances so lifecycle helpers can iterate without naming them.
const instances = [];

export function createDockableWindow(config) {
  const {
    title, className, geoKey, winStateKey, dockedKey, openKey,
    defaultW, defaultH, defaultY, build, dockOrder,
    getDockEl = () => dockEl,
    onUserClose,
    closable = false,
  } = config;

  let wb = null;
  let slot = null;
  let body = null;
  let off = null;
  let saved = null;
  let docking = false;  // float -> dock transition; onclose skips destroy
  let navAway = false;  // nav detach; onclose skips destroy
  let programmaticClose = false; // close() -> wb.close(); onclose skips onUserClose

  function loadWinState() { return localStorage.getItem(winStateKey); }
  function saveWinState(v) { if (v) localStorage.setItem(winStateKey, v); else localStorage.removeItem(winStateKey); }

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
    slot = makeDockSlot(title, body, undock, closable ? userClose : null);
    // Insert in dockOrder ascending; lower order goes on top.
    let inserted = false;
    for (const other of instances) {
      if (other === inst) continue;
      if (other.slot && other.slot.parentElement === container && other.dockOrder > dockOrder) {
        container.insertBefore(slot, other.slot);
        inserted = true;
        break;
      }
    }
    if (!inserted) container.appendChild(slot);
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
      ...winboxBase(title, className, w, h, x, y),
      mount: body,
      onclose() {
        if (wb) saveGeo(geoKey, wb);
        wb = null;
        if (docking || navAway) return; // body lives on
        // User-initiated close (WinBox X) when not flagged programmatic.
        // Persist the closed state so a hard refresh doesn't reopen.
        const userInitiated = !programmaticClose;
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
    addDockButton(wb, dock);
    const ws = loadWinState();
    if (ws === "min") wb.minimize();
    else if (ws === "max") wb.maximize();
  }

  function teardownSlot() {
    if (!slot) return;
    detachBody(slot, body);
    slot.remove();
    slot = null;
    if (off) { off(); off = null; }
    body = null;
  }

  function detachSlotForNav() {
    if (!slot) return;
    detachBody(slot, body);
    slot.remove();
    slot = null;
  }

  function close() {
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
    if (wb || slot) { close(); return; }
    setOpen(openKey, true);
    if (!body) body = build(events, { setOff });
    if (isDocked(dockedKey) && getDockEl()) {
      dock();
    } else {
      openFloat();
    }
  }

  function closeForNav() {
    if (wb) { saved = wbGeometry(wb); navAway = true; wb.close(); navAway = false; }
    detachSlotForNav();
  }

  function restore(events) {
    if (wb || slot) return; // already open from a prior call
    if (body || saved || isOpen(openKey)) toggle(events);
  }

  const inst = {
    toggle, close, teardownSlot, closeForNav, restore,
    get wb() { return wb; },
    get slot() { return slot; },
    get body() { return body; },
    dockOrder,
    sharesUciDock: !config.getDockEl,
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
    instances.forEach(i => { if (i.sharesUciDock) i.teardownSlot(); });
    dockGrip = null;
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

const UCI_GEO_KEY       = "sturddle:ucilog:geo";
const UCI_WIN_STATE_KEY = "sturddle:ucilog:winstate";
const UCI_DOCKED_KEY    = "sturddle:ucilog:docked";
const UCI_OPEN_KEY      = "sturddle:ucilog:open";

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
  const AUTOSCROLL_SLACK_PX = 4;
  setOff(events.on((evt) => {
    if (evt.kind !== "uci_log" || paused) return;
    const { dir, line } = evt.payload;
    // body.parentElement is wb.body when floating, .dock-slot-body when docked.
    const scroller = body.parentElement;
    const pinned = scroller
      ? scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - AUTOSCROLL_SLACK_PX
      : false;
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
    if (pinned && scroller) scroller.scrollTop = scroller.scrollHeight;
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
  dockOrder: 20, // below Search Lines
  closable: true,
});

// -- Search Lines body -------------------------------------------------------

const PV_GEO_KEY       = "sturddle:pvtable:geo";
const PV_WIN_STATE_KEY = "sturddle:pvtable:winstate";
const PV_DOCKED_KEY    = "sturddle:pvtable:docked";
const PV_OPEN_KEY      = "sturddle:pvtable:open";

function fmtScore(score) {
  if (!score) return "";
  if (score.mate != null) return `#${score.mate}`;
  return (score.cp / 100).toFixed(2);
}

function fmtK(n) {
  if (n == null) return "";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

function buildPvTableBody(events, { setOff }) {
  const body = document.createElement("div");
  body.className = "wb-pvtable";
  body.innerHTML = `
    <table class="wb-table wb-pvtable-tbl">
      <colgroup>
        <col class="wb-pvtable-col-depth">
        <col class="wb-pvtable-col-score">
        <col class="wb-pvtable-col-nodes">
        <col class="wb-pvtable-col-nps">
        <col class="wb-pvtable-col-pv">
      </colgroup>
      <thead>
        <tr>
          <th>Depth<span class="th-grip"></span></th>
          <th>Eval<span class="th-grip"></span></th>
          <th>Nodes<span class="th-grip"></span></th>
          <th>NPS<span class="th-grip"></span></th>
          <th>PV</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  `;

  const tbody = body.querySelector("tbody");
  const tableEl = body.querySelector(".wb-pvtable-tbl");
  const colEls = Array.from(body.querySelectorAll("col"));
  const COL_WIDTHS_KEY = "sturddle:pvtable:colWidths";
  const DEFAULT_WIDTHS = [50, 50, 55, 45];
  const minPx = 30;
  const grips = Array.from(body.querySelectorAll(".th-grip"));
  const colWidths = DEFAULT_WIDTHS.slice();

  attachColumnResize({
    table: tableEl,
    grips,
    overlayHost: body,
    storageKey: COL_WIDTHS_KEY,
    sizes: colWidths,
    unit: "px",
    dragLineHeight: () => {
      const tr = tableEl.getBoundingClientRect();
      const br = body.getBoundingClientRect();
      return Math.max(0, tr.bottom - br.top);
    },
    applySizes(sizes, ctx) {
      if (ctx) {
        const { deltaFrac, tableWidth, startSizes, gripIdx } = ctx;
        const d = deltaFrac * tableWidth;
        let a = startSizes[gripIdx] + d;
        if (a < minPx) a = minPx;
        sizes[gripIdx] = a;
        if (gripIdx + 1 < sizes.length) {
          let b = startSizes[gripIdx + 1] - d;
          if (b < minPx) b = minPx;
          sizes[gripIdx + 1] = b;
        }
      }
      // First 4 cols are fixed px; last col (PV) is auto to fill remaining space.
      colEls.slice(0, 4).forEach((c, i) => { c.style.width = sizes[i] + "px"; });
      colEls[4].style.width = "auto";
      const fixedW = sizes.reduce((s, w) => s + w, 0);
      tableEl.style.width = "100%";
      tableEl.style.minWidth = fixedW + "px";
      fitTableToPvContent();
    },
  });

  const rowMap = new Map();
  let maxDepth = 0;

  function clearTable() {
    tbody.textContent = "";
    rowMap.clear();
    maxDepth = 0;
  }

  setOff(events.on((evt) => {
    if (evt.kind !== "engine_info") return;
    const { depth, seldepth, score, nodes, nps, pv } = evt.payload;
    if (depth == null) return;
    // depth === 1 after maxDepth > 1 signals a new search (single-PV assumption;
    // MultiPV > 1 can emit low-depth lines mid-search and would false-trigger).
    if (depth === 1 && maxDepth > 1) clearTable();
    if (depth > maxDepth) maxDepth = depth;
    let tr = rowMap.get(depth);
    if (!tr) {
      tr = document.createElement("tr");
      tr.dataset.depth = String(depth);
      tr.innerHTML = `<td></td><td></td><td></td><td></td><td class="wb-pvtable-pv"></td>`;
      rowMap.set(depth, tr);
      // Insert sorted by depth descending (highest at top).
      let inserted = false;
      for (const row of tbody.rows) {
        if (Number(row.dataset.depth) < depth) {
          tbody.insertBefore(tr, row);
          inserted = true;
          break;
        }
      }
      if (!inserted) tbody.appendChild(tr);
    }
    tr.cells[0].textContent = seldepth != null ? `${depth}/${seldepth}` : depth;
    if (score) tr.cells[1].textContent = fmtScore(score);
    if (nodes != null) tr.cells[2].textContent = fmtK(nodes);
    if (nps != null) tr.cells[3].textContent = fmtK(nps);
    if (pv?.[0]) tr.cells[4].textContent = pv[0];
    fitTableToPvContent();
  }));

  // PV cell uses overflow:visible so long lines extend past the cell's
  // logical width. Grow the table to match so row borders and column
  // dividers extend to the right edge of the visible/scrollable content.
  function fitTableToPvContent() {
    let pvMax = 0;
    for (const row of tbody.rows) {
      const cell = row.cells[4];
      if (cell && cell.scrollWidth > pvMax) pvMax = cell.scrollWidth;
    }
    const fixedW = colWidths.reduce((s, w) => s + w, 0);
    tableEl.style.minWidth = (fixedW + pvMax) + "px";
  }

  return body;
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
  dockOrder: 10, // above UCI log
  closable: true,
});

// -- public API --------------------------------------------------------------

const VIEW_UCI_OPEN_KEY = "sturddle:view:ucilog:open";
const VIEW_PV_OPEN_KEY  = "sturddle:view:pvtable:open";

export function toggleUciLogWindow(events) { uciLog.toggle(events); }
export function togglePvTableWindow(events) { pvTable.toggle(events); }

export function closeDebugWindows() {
  instances.forEach(i => { if (i.sharesUciDock) i.closeForNav(); });
  syncDockVisibility();
}

export function closeDebugWindowsPersist() {
  instances.forEach(i => { if (i.sharesUciDock) i.close(); });
  syncDockVisibility();
}

export function restoreDebugWindows(events) {
  instances.forEach(i => { if (i.sharesUciDock) i.restore(events); });
}

// Save open state of debug windows as of the last view-mode analysis session.
export function snapshotViewAnalysisState() {
  setOpen(VIEW_UCI_OPEN_KEY, !!(uciLog.wb || uciLog.slot));
  setOpen(VIEW_PV_OPEN_KEY,  !!(pvTable.wb || pvTable.slot));
}

// Open debug windows based on the last view-mode analysis snapshot.
// Falls back to the shared open key on first use (before any snapshot exists).
export function restoreViewAnalysisWindows(events) {
  const uciShouldOpen = localStorage.getItem(VIEW_UCI_OPEN_KEY) !== null
    ? isOpen(VIEW_UCI_OPEN_KEY) : isOpen(UCI_OPEN_KEY);
  const pvShouldOpen  = localStorage.getItem(VIEW_PV_OPEN_KEY) !== null
    ? isOpen(VIEW_PV_OPEN_KEY)  : isOpen(PV_OPEN_KEY);
  if (uciShouldOpen && !uciLog.wb && !uciLog.slot) uciLog.toggle(events);
  if (pvShouldOpen  && !pvTable.wb && !pvTable.slot) pvTable.toggle(events);
}
