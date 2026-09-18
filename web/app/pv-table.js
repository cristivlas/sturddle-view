// Search Lines (PV) table: one row per depth iteration, newest depth on
// top, resizable columns. Shared by the play-perspective Search Lines
// window and the tournament live-game side panels.
//
// Feed-agnostic: callers subscribe to their own event source and push
// unified engine_info payloads via update(). The PV cell text differs by
// caller (SAN in play, joined UCI in tournaments), so it's passed in.
//
// `onActivate` (play only) turns showable rows into a double-click-to-play
// affordance; see docs/pv-play-spec.md. Tables created without it (the
// tournament live-game panels) get no hover/tooltip/double-click.

import { attachColumnResize } from "./col-resize.js";
import { fmtCount, fmtScore, markSelectable, rafCoalesce } from "./wb-utils.js";
import { pvFrames } from "./pv-walk.js";

const COL_MIN_PX = 30;
const DEFAULT_COL_WIDTHS = [50, 50, 55, 45];

// Whole-token match for SAN move-number tokens ("12." / "12...") emitted by
// python-chess's variation_san; UCI-only PV strings (tournament panels) have
// none, so every token there falls through to the move-span branch below.
const MOVE_NUM_RE = /^\d+\.(\.\.)?$/;

const SHOWABLE_CLASS = "wb-pv-showable";
const PLAYING_ROW_CLASS = "wb-pv-row-playing";
const MOVE_SPAN_CLASS = "wb-pv-move";
const MOVE_CURRENT_CLASS = "wb-pv-move-current";
const MOVE_NUM_CLASS = "wb-pv-movenum";
const SHOWABLE_TOOLTIP = "Double-click to play line";
// Fewer than two frames (searched position + at least one ply) has nothing
// to show: no hover, no tooltip, double-click inert.
const MIN_SHOWABLE_FRAMES = 2;
// A single click on a different row cancels the running show; a double-click
// retargets to it instead. A lone click can't tell which one it's part of
// yet, so the cancel is held for this long in case a second click (the
// dblclick event, which fires within this window) claims it first. Must not
// undercut the platform's own double-click window (Windows default 500ms),
// or a slow-but-valid double-click cancels first and then retargets: a
// jump, not a rewind.
const DBLCLICK_GRACE_MS = 500;

// Splits pvText into whitespace/move-number/move-token spans. Every SAN (or
// UCI) move token gets `data-ply` so a running show can highlight and scroll
// it into view; move-number tokens are styled but not ply-addressable. DOM
// nodes, not innerHTML, so no HTML-escaping concerns even though PV text is
// server-derived.
function renderPvInto(cell, pvText) {
  cell.textContent = "";
  const tokens = pvText.split(/(\s+)/);
  let ply = 0;
  for (const tok of tokens) {
    if (tok === "") continue;
    if (/^\s+$/.test(tok)) {
      cell.appendChild(document.createTextNode(tok));
      continue;
    }
    const span = document.createElement("span");
    if (MOVE_NUM_RE.test(tok)) {
      span.className = MOVE_NUM_CLASS;
    } else {
      span.className = MOVE_SPAN_CLASS;
      span.dataset.ply = String(ply);
      ply++;
    }
    span.textContent = tok;
    cell.appendChild(span);
  }
}

function clearPlyHighlight(tr) {
  const cur = tr.cells[4].querySelector(`.${MOVE_CURRENT_CLASS}`);
  cur?.classList.remove(MOVE_CURRENT_CLASS);
}

function highlightPly(tr, ply) {
  clearPlyHighlight(tr);
  const el = tr.cells[4].querySelector(`.${MOVE_SPAN_CLASS}[data-ply="${ply}"]`);
  if (!el) return;
  el.classList.add(MOVE_CURRENT_CLASS);
  el.scrollIntoView({ inline: "nearest", block: "nearest" });
}

// Per-row state (the {placement, pv_uci, frames} snapshot and the show
// generation counter used to detect a superseded same-row retarget) kept off
// the DOM node itself.
const rowState = new WeakMap();

function hasPlayableFrames(tr) {
  const st = rowState.get(tr);
  return !!st?.pvRow && st.pvRow.frames.length >= MIN_SHOWABLE_FRAMES;
}

// Live instances per colWidthsKey: a drag-end in one table pushes the new
// widths to every sibling sharing the key (stacked/side-by-side tables
// would otherwise misalign until recreated).
const syncRegistry = new Map(); // colWidthsKey -> Set<{applyExternal}>

export function createPvTable({ colWidthsKey, onActivate, cancelLine, canPlay }) {
  // Showable requires both a playable line AND the board currently willing
  // to play one (not analyzing, not editing) -- gating here, not just at
  // activation, means a row never becomes double-click-able (hover, tooltip)
  // when the click would only be refused; canPlay's answer can change
  // without a new pv_uci write, so refreshShowability() re-applies it.
  function isShowable(tr) {
    return hasPlayableFrames(tr) && (!canPlay || canPlay());
  }

  function applyShowability(tr) {
    const showable = isShowable(tr);
    tr.classList.toggle(SHOWABLE_CLASS, showable);
    if (showable) tr.title = SHOWABLE_TOOLTIP;
    else tr.removeAttribute("title");
  }

  function refreshShowability() {
    for (const tr of tbody.rows) {
      if (rowState.get(tr)?.pvRow) applyShowability(tr);
    }
  }

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
  markSelectable(tableEl);
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
  // The row currently playing a show, or null. Engine-info conflicts and
  // table clears pin this row instead of touching it (see update()/clear()).
  let playingRow = null;

  function activateRow(tr) {
    if (!onActivate || !isShowable(tr)) return;
    const depth = Number(tr.dataset.depth);
    const st = rowState.get(tr);
    const myShowGen = (st.showGen || 0) + 1;
    st.showGen = myShowGen;
    playingRow = tr;
    tr.classList.add(PLAYING_ROW_CLASS);
    const handle = {
      frames: st.pvRow.frames,
      pvUci: st.pvRow.pvUci,
      setPly(i) { highlightPly(tr, i); },
      release() {
        // A same-row retarget already bumped showGen for the new handle;
        // that new show owns the row's playing/highlight state now, so this
        // stale release() must leave it alone (checked before touching
        // anything, not just before the DOM removal at the end).
        if (rowState.get(tr)?.showGen !== myShowGen) return;
        tr.classList.remove(PLAYING_ROW_CLASS);
        clearPlyHighlight(tr);
        if (playingRow === tr) playingRow = null;
        if (rowMap.get(depth) !== tr && tr.parentElement === tbody) {
          tbody.removeChild(tr);
        }
      },
    };
    // Defense in depth: canPlay() already gates isShowable above, but if the
    // underlying play is refused anyway (a race with analyzing/editing
    // flipping, say), onActivate reports it and release() must still run --
    // nothing else will ever call it, and the row would otherwise stay
    // marked "playing" forever.
    if (onActivate(handle) === false) handle.release();
  }

  // A click on a different row than the one playing cancels it, but the
  // first click of a double-click looks identical to a lone click when it
  // fires -- hold the cancel for DBLCLICK_GRACE_MS so the dblclick handler
  // (which fires within that window) can claim it as a retarget instead.
  let pendingCancelTimer = null;

  function clearPendingCancel() {
    if (pendingCancelTimer) { clearTimeout(pendingCancelTimer); pendingCancelTimer = null; }
  }

  function attachRowActivation(tr) {
    if (!onActivate) return;
    tr.addEventListener("mousedown", (e) => {
      if (e.detail >= 2 && isShowable(tr)) e.preventDefault();
    });
    tr.addEventListener("click", () => {
      clearPendingCancel();
      if (!playingRow || playingRow === tr) return;
      pendingCancelTimer = setTimeout(() => {
        pendingCancelTimer = null;
        cancelLine?.();
      }, DBLCLICK_GRACE_MS);
    });
    tr.addEventListener("dblclick", () => {
      // Only a valid retarget target claims the pending single-click cancel
      // armed by this same sequence's clicks -- a double-click on a row with
      // nothing to show must not silently swallow it and leave the old show
      // stuck playing with no cancel in flight.
      if (!isShowable(tr)) return;
      clearPendingCancel();
      activateRow(tr);
    });
  }

  function clear() {
    if (playingRow) {
      // A table clear while a row plays is a conflict: pin it, drop the rest.
      for (const tr of Array.from(tbody.rows)) {
        if (tr !== playingRow) tbody.removeChild(tr);
      }
      rowMap.clear();
      maxDepth = 0;
      return;
    }
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

  function update(info, pvText, placement) {
    const { depth, seldepth, score, nodes, nps, pv_uci } = info;
    if (depth == null) return;
    // depth === 1 after maxDepth > 1 signals a new search (single-PV assumption;
    // MultiPV > 1 can emit low-depth lines mid-search and would false-trigger).
    if (depth === 1 && maxDepth > 1) clear();
    if (playingRow && rowMap.get(depth) === playingRow) {
      // The playing row is frozen whole: infos without a PV are dropped;
      // one carrying a PV is a conflict -- unkey and pin, a fresh row
      // follows below.
      if (!pv_uci || !pv_uci.length) return;
      rowMap.delete(depth);
    }
    if (depth > maxDepth) maxDepth = depth;
    let tr = rowMap.get(depth);
    if (!tr) {
      tr = document.createElement("tr");
      tr.dataset.depth = String(depth);
      tr.innerHTML = `<td></td><td></td><td></td><td></td><td class="wb-pvtable-pv"></td>`;
      rowState.set(tr, {});
      attachRowActivation(tr);
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
    if (pvText) renderPvInto(tr.cells[4], pvText);
    // A row's {placement, pv_uci, frames} is written as one unit, and only by
    // an info that carries pv_uci -- infos without one update depth/score/
    // nodes/nps only, so the triple never disagrees with itself.
    if (pv_uci && pv_uci.length) {
      rowState.get(tr).pvRow = { placement, pvUci: pv_uci, text: pvText, frames: pvFrames(placement, pv_uci) };
      applyShowability(tr);
    }
    fit();
  }

  function dispose() {
    clearPendingCancel();
    fit.cancel();
    colResize.destroy();
    siblings.delete(syncEntry);
    if (!siblings.size) syncRegistry.delete(colWidthsKey);
    cancelLine?.();
  }

  return { el, update, clear, fit, dispose, refreshShowability };
}
