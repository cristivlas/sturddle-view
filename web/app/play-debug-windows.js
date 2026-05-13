// Debug windows for play mode (desktop only).
// 1. UCI log: raw lines flowing between python-chess and the engine.
// 2. PV table: per-iteration principal variation, cutechess-style.

import { toast } from "./dialogs.js";

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
    class: `sturddle-wb ${className} no-full no-max`,
    width,
    height,
    minwidth: 320,
    minheight: 120,
    x,
    y,
    top: HEADER_H,
  };
}

// -- UCI log window ----------------------------------------------------------

let uciLogWb = null;

export function openUciLogWindow(events) {
  if (uciLogWb) {
    if (uciLogWb.min) uciLogWb.restore();
    uciLogWb.focus();
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

  const uciH = 320;
  const uciY = window.innerHeight - uciH - WIN_MARGIN;
  uciLogWb = new WinBox({
    ...winboxBase("UCI Log", "sturddle-wb-uci-log", rightColumnWidth(480), uciH, "right", uciY),
    mount: body,
    onclose() { offEvent(); uciLogWb = null; },
  });
}

// -- PV table window ---------------------------------------------------------

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
    return;
  }

  const body = document.createElement("div");
  body.className = "wb-pvtable";
  body.innerHTML = `
    <table class="wb-table wb-pvtable-tbl">
      <thead>
        <tr>
          <th>Depth</th><th>Score</th><th>Nodes</th><th>NPS</th><th>PV</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  `;

  const tbody = body.querySelector("tbody");
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

  const pvY = anchor ? Math.round(anchor.getBoundingClientRect().top) : HEADER_H;
  pvTableWb = new WinBox({
    ...winboxBase("PV Table", "sturddle-wb-pvtable", rightColumnWidth(560), 260, "right", pvY),
    mount: body,
    onclose() { offEvent(); pvTableWb = null; },
  });
}

export function closeDebugWindows() {
  uciLogWb?.close();
  pvTableWb?.close();
}
