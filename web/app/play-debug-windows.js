// Debug windows for play mode (desktop only).
// 1. UCI log: raw lines flowing between python-chess and the engine.
// 2. Search Lines: per-iteration principal variation, cutechess-style.
//
// Each window can float (WinBox) or dock into the left column of the play
// grid (.play-dock-left). Dock state is persisted in localStorage.

import { toast } from "./dialogs.js";

const UCI_LOG_MAX_LINES = 1000;
// Once the buffer overflows, trim this many lines in one go instead of
// one-per-incoming-line -- amortizes the layout cost at high info rates.
const UCI_LOG_TRIM_CHUNK = 100;
const HEADER_H = 44; // px -- approximate nav header height
const WIN_MARGIN = 8; // gap between window edge and WinBox

// Set by play.js on perspective mount/unmount.
let _dockEl = null;
let _dockResizeObs = null;

export function setDockContainer(el) {
  if (_dockResizeObs) { _dockResizeObs.disconnect(); _dockResizeObs = null; }
  window.removeEventListener("resize", _updateDockBounds);
  _dockEl = el;
  if (el) {
    const board = document.querySelector(".play-board-host");
    if (board) {
      _dockResizeObs = new ResizeObserver(_updateDockBounds);
      _dockResizeObs.observe(board);
    }
    window.addEventListener("resize", _updateDockBounds);
    _updateDockBounds();
  }
  _syncDockVisibility();
}

function _isMobileLayout() {
  return window.innerWidth <= 800 || window.innerHeight <= 700;
}

function _updateDockBounds() {
  if (!_dockEl || _isMobileLayout()) return;
  const board = document.querySelector(".play-board-host");
  if (!board) return;
  const boardLeft = Math.round(board.getBoundingClientRect().left);
  const ribbonW = parseInt(getComputedStyle(_dockEl.closest(".play-grid") ?? document.documentElement)
    .getPropertyValue("--ribbon-w")) || 36;
  _dockEl.style.width = (boardLeft - ribbonW - 9) + "px";

  const clockTop = document.querySelector(".clock-row.clock-top");
  const clockBot = document.querySelector(".clock-row.clock-bottom");
  if (clockTop && clockBot) {
    const top = Math.round(clockTop.getBoundingClientRect().top);
    const bot = Math.round(clockBot.getBoundingClientRect().bottom);
    _dockEl.style.top    = top + "px";
    _dockEl.style.bottom = (window.innerHeight - bot) + "px";
    _dockEl.style.height = "";
  }
}

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

// -- saved geometry for navigation-away restore ------------------------------

const UCI_GEO_KEY    = "sturddle.ucilog.geo";
const PV_GEO_KEY     = "sturddle.pvtable.geo";
const UCI_DOCKED_KEY = "sturddle.ucilog.docked";
const PV_DOCKED_KEY  = "sturddle.pvtable.docked";
const UCI_OPEN_KEY   = "sturddle.ucilog.open";
const PV_OPEN_KEY    = "sturddle.pvtable.open";

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

// -- dock container ----------------------------------------------------------

// Each docked window gets a .dock-slot child inside _dockEl.
// slot structure:
//   .dock-slot
//     .dock-slot-header  (title + undock button)
//     .dock-slot-body    (the window's body div, transplanted here)

function _syncDockVisibility() {
  if (!_dockEl) return;
  const hasSlots = _dockEl.querySelector(".dock-slot") !== null;
  _dockEl.classList.toggle("dock-empty", !hasSlots);
}

function _makeDockSlot(title, bodyEl, onUndock) {
  const slot = document.createElement("div");
  slot.className = "dock-slot";

  const header = document.createElement("div");
  header.className = "dock-slot-header";

  const titleSpan = document.createElement("span");
  titleSpan.className = "dock-slot-title";
  titleSpan.textContent = title;

  const undockBtn = document.createElement("button");
  undockBtn.type = "button";
  undockBtn.className = "dock-slot-undock";
  undockBtn.title = "Undock";
  undockBtn.setAttribute("aria-label", "Undock");
  undockBtn.innerHTML = `<wa-icon name="arrow-up-right-from-square"></wa-icon>`;
  undockBtn.addEventListener("click", onUndock);

  header.append(titleSpan, undockBtn);

  const bodyWrap = document.createElement("div");
  bodyWrap.className = "dock-slot-body";
  bodyWrap.appendChild(bodyEl);

  slot.append(header, bodyWrap);
  return slot;
}

function _addDockButton(wb, onDock) {
  wb.addControl({ class: "wb-dock-ctrl", index: 0, click: onDock });
}

// -- UCI log window ----------------------------------------------------------

let uciLogWb   = null;
let uciLogSlot = null; // .dock-slot element when docked
let uciLogBody = null; // the persistent body div (shared between float/dock)
let uciLogOff  = null; // event unsubscribe fn
let uciLogSaved = null;

function _uciLogScrollContainer() {
  // When floating, WinBox owns the scroll (wb.body); when docked the
  // .dock-slot-body div is the scroller.
  if (uciLogWb) return uciLogWb.body;
  if (uciLogSlot) return uciLogSlot.querySelector(".dock-slot-body");
  return null;
}

function _buildUciLogBody(events) {
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

  const AUTOSCROLL_SLACK_PX = 4;
  uciLogOff = events.on((evt) => {
    if (evt.kind !== "uci_log" || paused) return;
    const { dir, line } = evt.payload;
    const scroller = _uciLogScrollContainer();
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
  });

  return body;
}

function _dockUciLog() {
  if (!_dockEl) return;
  if (uciLogWb) {
    saveGeo(UCI_GEO_KEY, uciLogWb);
    uciLogWb.body.removeChild(uciLogBody);
    const wb = uciLogWb;
    uciLogWb = null; // null first so onclose skips _destroyUciLog
    wb.close();
  }
  setDocked(UCI_DOCKED_KEY, true);
  uciLogSlot = _makeDockSlot("UCI Log", uciLogBody, _undockUciLog);
  _dockEl.appendChild(uciLogSlot);
  _syncDockVisibility();
}

function _undockUciLog() {
  if (!uciLogSlot) return;
  // Detach body from dock slot before removing the slot.
  uciLogSlot.querySelector(".dock-slot-body").removeChild(uciLogBody);
  uciLogSlot.remove();
  uciLogSlot = null;
  setDocked(UCI_DOCKED_KEY, false);
  _syncDockVisibility();
  _openUciLogFloat();
}

function _openUciLogFloat() {
  const uciGeo = uciLogSaved ?? loadGeo(UCI_GEO_KEY);
  const uciH = uciGeo?.height ?? 320;
  const uciW = uciGeo?.width ?? rightColumnWidth(480);
  const uciX = uciGeo?.x ?? "right";
  const uciY = uciGeo?.y ?? (() => {
    const clockBottom = document.querySelector(".clock-row.clock-bottom");
    const clockTop = clockBottom ? Math.round(clockBottom.getBoundingClientRect().top) : window.innerHeight;
    return clockTop - uciH - WIN_MARGIN;
  })();
  uciLogSaved = null;
  uciLogWb = new WinBox({
    ...winboxBase("UCI Log", "sturddle-wb-uci-log", uciW, uciH, uciX, uciY),
    mount: uciLogBody,
    onclose() { saveGeo(UCI_GEO_KEY, uciLogWb); _destroyUciLog(); },
    onmove()   { saveGeo(UCI_GEO_KEY, uciLogWb); },
    onresize() { saveGeo(UCI_GEO_KEY, uciLogWb); },
  });
  _addDockButton(uciLogWb, _dockUciLog);
}

function _destroyUciLog() {
  if (!uciLogWb) return; // already cleared (docking path)
  if (uciLogOff) { uciLogOff(); uciLogOff = null; }
  uciLogWb   = null;
  uciLogBody = null;
}

function _closeUciLog() {
  localStorage.setItem(UCI_OPEN_KEY, "0");
  if (uciLogWb) { uciLogWb.close(); return; }
  if (uciLogSlot) {
    uciLogSlot.querySelector(".dock-slot-body").removeChild(uciLogBody);
    uciLogSlot.remove();
    uciLogSlot = null;
    if (uciLogOff) { uciLogOff(); uciLogOff = null; }
    uciLogBody = null;
    _syncDockVisibility();
  }
}

export function toggleUciLogWindow(events) {
  if (uciLogWb || uciLogSlot) { _closeUciLog(); return; }
  localStorage.setItem(UCI_OPEN_KEY, "1");
  uciLogBody = _buildUciLogBody(events);
  if (isDocked(UCI_DOCKED_KEY) && _dockEl) {
    _dockUciLog();
  } else {
    _openUciLogFloat();
  }
}

// -- Search Lines window -----------------------------------------------------

let pvTableWb   = null;
let pvTableSlot = null;
let pvTableBody = null;
let pvTableOff  = null;
let pvTableSaved = null;

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

function _buildPvTableBody(events) {
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
  const COL_WIDTHS_KEY = "sturddle.pvtable.colWidths";
  const DEFAULT_WIDTHS = [50, 65, 65, 65];
  let colWidths = DEFAULT_WIDTHS.slice();
  try {
    const saved = JSON.parse(localStorage.getItem(COL_WIDTHS_KEY));
    if (Array.isArray(saved) && saved.length === 4) colWidths = saved;
  } catch (e) { /* use defaults */ }

  function applyColWidths() {
    // First 4 cols are fixed px; last col (PV) is auto to fill remaining space.
    colEls.slice(0, 4).forEach((c, i) => { c.style.width = colWidths[i] + "px"; });
    colEls[4].style.width = "auto";
    const fixedW = colWidths.reduce((s, w) => s + w, 0);
    tableEl.style.width = "100%";
    tableEl.style.minWidth = fixedW + "px";
  }
  applyColWidths();

  const minPx = 30;
  body.querySelectorAll(".th-grip").forEach((grip, gripIdx) => {
    grip.addEventListener("pointerdown", (eDown) => {
      if (eDown.button !== 0) return;
      eDown.preventDefault();
      grip.setPointerCapture(eDown.pointerId);
      grip.classList.add("dragging");
      const startX = eDown.clientX;
      const startA = colWidths[gripIdx];
      const startB = gripIdx + 1 < colWidths.length ? colWidths[gripIdx + 1] : null;

      const rightLine = document.createElement("div");
      const leftLine = document.createElement("div");
      rightLine.className = leftLine.className = "col-drag-line";
      rightLine.style.top = leftLine.style.top = "0";
      body.appendChild(rightLine);
      body.appendChild(leftLine);

      function placeLines(clientX) {
        const bodyLeft = body.getBoundingClientRect().left;
        const thLeft = tableEl.querySelectorAll("thead th")[gripIdx].getBoundingClientRect().left;
        rightLine.style.left = (clientX - bodyLeft) + "px";
        rightLine.style.height = leftLine.style.height = body.scrollHeight + "px";
        leftLine.style.left = (thLeft - bodyLeft) + "px";
      }
      placeLines(eDown.clientX);

      function onMove(e) {
        const d = e.clientX - startX;
        let a = startA + d;
        if (a < minPx) a = minPx;
        colWidths[gripIdx] = a;
        if (startB !== null) {
          let b = startB - d;
          if (b < minPx) b = minPx;
          colWidths[gripIdx + 1] = b;
        }
        applyColWidths();
        placeLines(e.clientX);
      }
      let done = false;
      function onUp() {
        if (done) return;
        done = true;
        grip.classList.remove("dragging");
        rightLine.remove();
        leftLine.remove();
        localStorage.setItem(COL_WIDTHS_KEY, JSON.stringify(colWidths.slice(0, 4)));
        grip.removeEventListener("pointermove", onMove);
        grip.removeEventListener("pointerup", onUp);
        grip.removeEventListener("pointercancel", onUp);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onUp);
      }
      grip.addEventListener("pointermove", onMove);
      grip.addEventListener("pointerup", onUp);
      grip.addEventListener("pointercancel", onUp);
      document.addEventListener("pointerup", onUp);
      document.addEventListener("pointercancel", onUp);
    });
  });

  const rowMap = new Map();
  let maxDepth = 0;

  function clearTable() {
    tbody.textContent = "";
    rowMap.clear();
    maxDepth = 0;
  }

  pvTableOff = events.on((evt) => {
    if (evt.kind !== "engine_info") return;
    const { depth, score, nodes, nps, pv } = evt.payload;
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
    tr.cells[0].textContent = depth;
    if (score) tr.cells[1].textContent = fmtScore(score);
    if (nodes != null) tr.cells[2].textContent = fmtK(nodes);
    if (nps != null) tr.cells[3].textContent = fmtK(nps);
    if (pv?.[0]) tr.cells[4].textContent = pv[0];
  });

  return body;
}

function _dockPvTable() {
  if (!_dockEl) return;
  if (pvTableWb) {
    saveGeo(PV_GEO_KEY, pvTableWb);
    pvTableWb.body.removeChild(pvTableBody);
    const wb = pvTableWb;
    pvTableWb = null; // null first so onclose skips _destroyPvTable
    wb.close();
  }
  setDocked(PV_DOCKED_KEY, true);
  pvTableSlot = _makeDockSlot("Search Lines", pvTableBody, _undockPvTable);
  // Search Lines goes above UCI log when both are docked.
  const uciSlot = uciLogSlot;
  if (uciSlot) {
    _dockEl.insertBefore(pvTableSlot, uciSlot);
  } else {
    _dockEl.appendChild(pvTableSlot);
  }
  _syncDockVisibility();
}

function _undockPvTable() {
  if (!pvTableSlot) return;
  pvTableSlot.querySelector(".dock-slot-body").removeChild(pvTableBody);
  pvTableSlot.remove();
  pvTableSlot = null;
  setDocked(PV_DOCKED_KEY, false);
  _syncDockVisibility();
  _openPvTableFloat();
}

function _openPvTableFloat() {
  const pvGeo = pvTableSaved ?? loadGeo(PV_GEO_KEY);
  const pvH = pvGeo?.height ?? 260;
  const pvW = pvGeo?.width ?? rightColumnWidth(560);
  const pvX = pvGeo?.x ?? "right";
  const pvY = pvGeo?.y ?? HEADER_H;
  pvTableSaved = null;
  pvTableWb = new WinBox({
    ...winboxBase("Search Lines", "sturddle-wb-pvtable", pvW, pvH, pvX, pvY),
    mount: pvTableBody,
    onclose() { saveGeo(PV_GEO_KEY, pvTableWb); _destroyPvTable(); },
    onmove()   { saveGeo(PV_GEO_KEY, pvTableWb); },
    onresize() { saveGeo(PV_GEO_KEY, pvTableWb); },
  });
  _addDockButton(pvTableWb, _dockPvTable);
}

function _destroyPvTable() {
  if (!pvTableWb) return; // already cleared (docking path)
  if (pvTableOff) { pvTableOff(); pvTableOff = null; }
  pvTableWb   = null;
  pvTableBody = null;
}

function _closePvTable() {
  localStorage.setItem(PV_OPEN_KEY, "0");
  if (pvTableWb) { pvTableWb.close(); return; }
  if (pvTableSlot) {
    pvTableSlot.querySelector(".dock-slot-body").removeChild(pvTableBody);
    pvTableSlot.remove();
    pvTableSlot = null;
    if (pvTableOff) { pvTableOff(); pvTableOff = null; }
    pvTableBody = null;
    _syncDockVisibility();
  }
}

export function togglePvTableWindow(events) {
  if (pvTableWb || pvTableSlot) { _closePvTable(); return; }
  localStorage.setItem(PV_OPEN_KEY, "1");
  pvTableBody = _buildPvTableBody(events);
  if (isDocked(PV_DOCKED_KEY) && _dockEl) {
    _dockPvTable();
  } else {
    _openPvTableFloat();
  }
}

// -- lifecycle ---------------------------------------------------------------

export function closeDebugWindows() {
  if (uciLogWb)   { uciLogSaved = wbGeometry(uciLogWb); uciLogWb.close(); }
  if (pvTableWb)  { pvTableSaved = wbGeometry(pvTableWb); pvTableWb.close(); }
  // Tear down docked windows too (perspective navigating away).
  if (uciLogSlot) {
    uciLogSlot.querySelector(".dock-slot-body").removeChild(uciLogBody);
    uciLogSlot.remove();
    uciLogSlot = null;
    if (uciLogOff) { uciLogOff(); uciLogOff = null; }
    uciLogBody = null;
  }
  if (pvTableSlot) {
    pvTableSlot.querySelector(".dock-slot-body").removeChild(pvTableBody);
    pvTableSlot.remove();
    pvTableSlot = null;
    if (pvTableOff) { pvTableOff(); pvTableOff = null; }
    pvTableBody = null;
  }
  _syncDockVisibility();
}

export function restoreDebugWindows(events) {
  const uciOpen = localStorage.getItem(UCI_OPEN_KEY);
  const pvOpen  = localStorage.getItem(PV_OPEN_KEY);
  // null = never opened (first visit) => closed; "1" = user opened.
  if (uciLogSaved || uciOpen === "1") toggleUciLogWindow(events);
  if (pvTableSaved || pvOpen  === "1") togglePvTableWindow(events);
}
