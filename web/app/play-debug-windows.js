// Debug windows for play mode (desktop only).
// 1. UCI log: raw lines flowing between python-chess and the engine.
// 2. Search Lines: per-iteration principal variation, cutechess-style.

import { toast } from "./dialogs.js";
import { flashWindow } from "./wb-utils.js";

const UCI_LOG_MAX_LINES = 1000;
// Once the buffer overflows, trim this many lines in one go instead of
// one-per-incoming-line -- amortizes the layout cost at high info rates.
const UCI_LOG_TRIM_CHUNK = 100;
const HEADER_H = 44; // px -- approximate nav header height
const WIN_MARGIN = 8; // gap between window edge and WinBox

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

const UCI_GEO_KEY = "sturddle.ucilog.geo";
const PV_GEO_KEY  = "sturddle.pvtable.geo";

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

let uciLogSaved = null;
let pvTableSaved = null;

// -- UCI log window ----------------------------------------------------------

let uciLogWb = null;

export function openUciLogWindow(events) {
  if (uciLogWb) {
    if (uciLogWb.min) uciLogWb.restore();
    uciLogWb.focus();
    flashWindow(uciLogWb);
    return;
  }

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
  const offEvent = events.on((evt) => {
    if (evt.kind !== "uci_log" || paused) return;
    const { dir, line } = evt.payload;
    const scroller = uciLogWb.body;
    const pinned = scroller.scrollTop + scroller.clientHeight
      >= scroller.scrollHeight - AUTOSCROLL_SLACK_PX;
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
    if (pinned) scroller.scrollTop = scroller.scrollHeight;
  });

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
    mount: body,
    onclose() { offEvent(); saveGeo(UCI_GEO_KEY, uciLogWb); uciLogWb = null; },
    onmove()   { saveGeo(UCI_GEO_KEY, uciLogWb); },
    onresize() { saveGeo(UCI_GEO_KEY, uciLogWb); },
  });
}

// -- Search Lines window -----------------------------------------------------

let pvTableWb = null;

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

export function openPvTableWindow(events, anchor = null) {
  if (pvTableWb) {
    if (pvTableWb.min) pvTableWb.restore();
    pvTableWb.focus();
    flashWindow(pvTableWb);
    return;
  }

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
  const COL_PCTS_KEY = "sturddle.pvtable.colPcts";
  const DEFAULT_PCTS = [9, 12, 12, 12, 55];
  let colPcts = DEFAULT_PCTS.slice();
  try {
    const saved = JSON.parse(localStorage.getItem(COL_PCTS_KEY));
    if (Array.isArray(saved) && saved.length === 5) colPcts = saved;
  } catch (e) { /* use defaults */ }

  function applyColPcts() {
    colEls.forEach((c, i) => { c.style.width = colPcts[i] + "%"; });
  }
  applyColPcts();

  const minPct = 5;
  body.querySelectorAll(".th-grip").forEach((grip, gripIdx) => {
    grip.addEventListener("pointerdown", (eDown) => {
      if (eDown.button !== 0) return;
      eDown.preventDefault();
      grip.setPointerCapture(eDown.pointerId);
      grip.classList.add("dragging");
      const startX = eDown.clientX;
      const startA = colPcts[gripIdx], startB = colPcts[gripIdx + 1];
      const tableW = tableEl.getBoundingClientRect().width || 1;

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
        const dPct = ((e.clientX - startX) / tableW) * 100;
        let a = startA + dPct, b = startB - dPct;
        if (a < minPct) { b -= minPct - a; a = minPct; }
        if (b < minPct) { a -= minPct - b; b = minPct; }
        colPcts[gripIdx] = a; colPcts[gripIdx + 1] = b;
        applyColPcts();
        placeLines(e.clientX);
      }
      let done = false;
      function onUp() {
        if (done) return;
        done = true;
        grip.classList.remove("dragging");
        rightLine.remove();
        leftLine.remove();
        localStorage.setItem(COL_PCTS_KEY, JSON.stringify(colPcts));
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

  // depth -> <tr>
  const rowMap = new Map();
  let maxDepth = 0;

  function clearTable() {
    tbody.textContent = "";
    rowMap.clear();
    maxDepth = 0;
  }

  const offEvent = events.on((evt) => {
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

  const pvGeo = pvTableSaved ?? loadGeo(PV_GEO_KEY);
  const pvH = pvGeo?.height ?? 260;
  const pvW = pvGeo?.width ?? rightColumnWidth(560);
  const pvX = pvGeo?.x ?? "right";
  const pvY = pvGeo?.y ?? (anchor ? Math.round(anchor.getBoundingClientRect().top) : HEADER_H);
  pvTableSaved = null;
  pvTableWb = new WinBox({
    ...winboxBase("Search Lines", "sturddle-wb-pvtable", pvW, pvH, pvX, pvY),
    mount: body,
    onclose() { offEvent(); saveGeo(PV_GEO_KEY, pvTableWb); pvTableWb = null; },
    onmove()   { saveGeo(PV_GEO_KEY, pvTableWb); },
    onresize() { saveGeo(PV_GEO_KEY, pvTableWb); },
  });
}

export function closeDebugWindows() {
  if (uciLogWb) { uciLogSaved = wbGeometry(uciLogWb); uciLogWb.close(); }
  if (pvTableWb) { pvTableSaved = wbGeometry(pvTableWb); pvTableWb.close(); }
}

export function restoreDebugWindows(events, anchor = null) {
  if (uciLogSaved) openUciLogWindow(events);
  if (pvTableSaved) openPvTableWindow(events, anchor);
}
