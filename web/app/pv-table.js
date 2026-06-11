// Search Lines (PV) table: one row per depth iteration, newest depth on
// top, resizable columns. Shared by the play-perspective Search Lines
// window and the tournament live-game side panels.
//
// Feed-agnostic: callers subscribe to their own event source and push
// unified engine_info payloads via update(). The PV cell text differs by
// caller (SAN in play, joined UCI in tournaments), so it's passed in.

import { attachColumnResize } from "./col-resize.js";
import { fmtCount, fmtScore, rafCoalesce } from "./wb-utils.js";

const COL_MIN_PX = 30;
const DEFAULT_COL_WIDTHS = [50, 50, 55, 45];

// Live instances per colWidthsKey: a drag-end in one table pushes the new
// widths to every sibling sharing the key (stacked/side-by-side tables
// would otherwise misalign until recreated).
const syncRegistry = new Map(); // colWidthsKey -> Set<{applyExternal}>

export function createPvTable({ colWidthsKey }) {
  const el = document.createElement("div");
  el.className = "wb-pvtable";
  el.innerHTML = `
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

  const tbody = el.querySelector("tbody");
  const tableEl = el.querySelector(".wb-pvtable-tbl");
  const colEls = Array.from(el.querySelectorAll("col"));
  const grips = Array.from(el.querySelectorAll(".th-grip"));
  const colWidths = DEFAULT_COL_WIDTHS.slice();

  function applySizes(sizes, ctx) {
    if (ctx) {
      const { deltaFrac, tableWidth, startSizes, gripIdx } = ctx;
      const d = deltaFrac * tableWidth;
      let a = startSizes[gripIdx] + d;
      if (a < COL_MIN_PX) a = COL_MIN_PX;
      sizes[gripIdx] = a;
      if (gripIdx + 1 < sizes.length) {
        let b = startSizes[gripIdx + 1] - d;
        if (b < COL_MIN_PX) b = COL_MIN_PX;
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
  }

  const syncEntry = {
    applyExternal(sizes) {
      for (let i = 0; i < colWidths.length; i++) colWidths[i] = sizes[i];
      applySizes(colWidths);
    },
  };
  let siblings = syncRegistry.get(colWidthsKey);
  if (!siblings) { siblings = new Set(); syncRegistry.set(colWidthsKey, siblings); }
  siblings.add(syncEntry);

  const colResize = attachColumnResize({
    table: tableEl,
    grips,
    overlayHost: el,
    storageKey: colWidthsKey,
    sizes: colWidths,
    unit: "px",
    dragLineHeight: () => {
      const tr = tableEl.getBoundingClientRect();
      const br = el.getBoundingClientRect();
      return Math.max(0, tr.bottom - br.top);
    },
    applySizes,
    onSave(sizes) {
      for (const entry of siblings) {
        if (entry !== syncEntry) entry.applyExternal(sizes);
      }
    },
  });

  const rowMap = new Map();
  let maxDepth = 0;

  function clear() {
    tbody.textContent = "";
    rowMap.clear();
    maxDepth = 0;
  }

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
  // Coalesced: scrollWidth is a layout probe; per-info-event sync reads
  // would force a reflow on every engine info line.
  const fit = rafCoalesce(fitTableToPvContent);

  function update(info, pvText) {
    const { depth, seldepth, score, nodes, nps } = info;
    if (depth == null) return;
    // depth === 1 after maxDepth > 1 signals a new search (single-PV assumption;
    // MultiPV > 1 can emit low-depth lines mid-search and would false-trigger).
    if (depth === 1 && maxDepth > 1) clear();
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
    if (nodes != null) tr.cells[2].textContent = fmtCount(nodes);
    if (nps != null) tr.cells[3].textContent = fmtCount(nps);
    if (pvText) tr.cells[4].textContent = pvText;
    fit();
  }

  function dispose() {
    fit.cancel();
    colResize.destroy();
    siblings.delete(syncEntry);
    if (!siblings.size) syncRegistry.delete(colWidthsKey);
  }

  return { el, update, clear, fit, dispose };
}
