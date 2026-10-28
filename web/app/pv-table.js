// Search Lines (PV) table: one row per depth iteration (per depth and
// MultiPV line once the engine reports several), newest depth on top.
// Columns (column-header.js): resizable px widths, reorderable, Eval/Nodes/
// NPS hideable, auto-fit, PV pinned last and filling the rest; layout shared live by
// tables on one layoutKey. Shared by the play-perspective Search Lines
// window and the tournament live-game side panels.
//
// Feed-agnostic: callers subscribe to their own event source and push
// unified engine_info payloads via update(). The PV cell text differs by
// caller (SAN in play, joined UCI in tournaments), so it's passed in; the
// searched position is sampled from `currentPlacement` (play only).
//
// `onActivate` (play only) turns showable rows into a double-click-to-play
// affordance; see docs/pv-play-spec.md. Tables created without it (the
// tournament live-game panels) get no hover/tooltip/double-click.

import { fmtCount, fmtScore, markSelectable, rafCoalesce } from "./wb-utils.js";
import { pvFrames } from "./pv-walk.js";
import { BEST_RANK } from "./chess-consts.js";
import { createColumnHeader, WIDTH_UNIT } from "./column-header.js";

const COL = Object.freeze({ DEPTH: "depth", EVAL: "eval", NODES: "nodes", NPS: "nps", PV: "pv" });
const PV_CELL_CLASS = "wb-pvtable-pv";
// Default px widths of the fixed columns.
const DEFAULT_COL_WIDTHS = Object.freeze({ [COL.DEPTH]: 50, [COL.EVAL]: 50, [COL.NODES]: 55, [COL.NPS]: 45 });
const COLUMNS = [
  { key: COL.DEPTH, label: "Depth", width: DEFAULT_COL_WIDTHS[COL.DEPTH], alwaysOn: true },
  { key: COL.EVAL, label: "Eval", width: DEFAULT_COL_WIDTHS[COL.EVAL] },
  { key: COL.NODES, label: "Nodes", width: DEFAULT_COL_WIDTHS[COL.NODES] },
  { key: COL.NPS, label: "NPS", width: DEFAULT_COL_WIDTHS[COL.NPS] },
  { key: COL.PV, label: "PV", pinEnd: true, colClass: "wb-pvtable-col-pv" },
];
// Saved widths from before the column layout: a px array, in this order.
const LEGACY_WIDTH_KEYS = [COL.DEPTH, COL.EVAL, COL.NODES, COL.NPS];
const migrateLegacyWidths = (saved) => (Array.isArray(saved)
  ? { unit: WIDTH_UNIT.PX, widths: Object.fromEntries(LEGACY_WIDTH_KEYS.map((k, i) => [k, saved[i]])) }
  : saved);

// Whole-token match for SAN move-number tokens ("12." / "12...") emitted by
// python-chess's variation_san; UCI-only PV strings (tournament panels) have
// none, so every token there falls through to the move-span branch below.
const MOVE_NUM_RE = /^\d+\.(\.\.)?$/;

const SHOWABLE_CLASS = "wb-pv-showable";
const PLAYING_ROW_CLASS = "wb-pv-row-playing";
const MOVE_SPAN_CLASS = "wb-pv-move";
const MOVE_CURRENT_CLASS = "wb-pv-move-current";
const MOVE_NUM_CLASS = "wb-pv-movenum";
const RANK_CLASS = "wb-pv-rank";
export const SHOWABLE_TOOLTIP = "Double-click to play line";
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

// A row's cell for column `key`, shown or not.
const cellOf = (tr, key) => rowState.get(tr).cells.get(key);

// MultiPV line number, a circle ahead of the PV moves (renderPvInto wipes it).
function setRankBadge(tr, rank) {
  const cell = cellOf(tr, COL.PV);
  let badge = cell.querySelector(`.${RANK_CLASS}`);
  if (!badge) {
    badge = document.createElement("span");
    badge.className = RANK_CLASS;
    cell.prepend(badge);
  }
  badge.textContent = rank;
}

function clearPlyHighlight(tr) {
  const cur = cellOf(tr, COL.PV).querySelector(`.${MOVE_CURRENT_CLASS}`);
  cur?.classList.remove(MOVE_CURRENT_CLASS);
}

function highlightPly(tr, ply) {
  clearPlyHighlight(tr);
  const el = cellOf(tr, COL.PV).querySelector(`.${MOVE_SPAN_CLASS}[data-ply="${ply}"]`);
  if (!el) return;
  el.classList.add(MOVE_CURRENT_CLASS);
  el.scrollIntoView({ inline: "nearest", block: "nearest" });
}

// Per-row state off the DOM node: depth, rank, td per column key (hidden ones
// detached but kept current), the {placement, pv_uci, frames} snapshot, and
// the show generation that detects a superseded same-row retarget.
const rowState = new WeakMap();

function hasPlayableFrames(tr) {
  const st = rowState.get(tr);
  return !!st?.pvRow && st.pvRow.frames.length >= MIN_SHOWABLE_FRAMES;
}

export function createPvTable({ layoutKey, onActivate, cancelLine, canPlay, currentPlacement }) {
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

  function writePvRow(tr, placement, pvUci, text) {
    rowState.get(tr).pvRow = { placement, pvUci, text, frames: pvFrames(placement, pvUci) };
  }

  // A row written with no board to sample (the view unmounted; this body
  // outlives a perspective nav) has no frames. The gate is re-announced on
  // remount, so resample it against the live board then; pvFrames truncates
  // if the position has moved on.
  function resampleUnsampled(tr) {
    const st = rowState.get(tr);
    if (st.pvRow.placement) return;
    const placement = currentPlacement?.();
    if (placement) writePvRow(tr, placement, st.pvRow.pvUci, st.pvRow.text);
  }

  function refreshShowability() {
    for (const tr of tbody.rows) {
      if (!rowState.get(tr)?.pvRow) continue;
      resampleUnsampled(tr);
      applyShowability(tr);
    }
  }

  const rowMap = new Map(); // rowKey(depth, rank) -> tr
  // The shown columns, in order (set on every header rebuild).
  let shown = [];

  // Put each row's tds in the shown order; hidden ones drop out of the DOM.
  function layoutRow(tr) {
    const { cells } = rowState.get(tr);
    tr.replaceChildren(...shown.map((c) => cells.get(c.key)));
  }

  const header = createColumnHeader({
    columns: COLUMNS,
    layoutKey,
    unit: WIDTH_UNIT.PX,
    traits: { resize: true, reorder: true, hide: true, fit: true },
    classes: {
      wrap: "wb-pvtable",
      headScroll: "wb-pvtable-head-scroll",
      headTable: "wb-table wb-pvtable-head-tbl",
      bodyScroll: "wb-pvtable-scroll",
      bodyTable: "wb-table wb-pvtable-tbl",
    },
    sync: true,
    migrate: migrateLegacyWidths,
    dragLineHeight: () => {
      const tr = tableEl.getBoundingClientRect();
      const br = el.getBoundingClientRect();
      return Math.max(0, tr.bottom - br.top);
    },
    onSizes: () => fitTableToPvContent(),
    onLayout(cols) {
      shown = cols;
      for (const tr of tbody.rows) layoutRow(tr);
      fit();
    },
  });
  const { wrap: el, headTable: headTableEl, bodyTable: tableEl, tbody, bodyScroll: scrollEl } = header;
  markSelectable(tableEl);
  markSelectable(headTableEl);

  let maxDepth = 0;
  // A line below the best showed up this search: rows show their line
  // number (none until then, so single-PV searches show none).
  let ranked = false;
  // The row currently playing a show, or null. Engine-info conflicts and
  // table clears pin this row instead of touching it (see update()/clear()).
  let playingRow = null;

  function activateRow(tr) {
    if (!onActivate || !isShowable(tr)) return;
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
        if (rowMap.get(rowKey(st.depth, st.rank)) !== tr && tr.parentElement === tbody) {
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
      // The scroll stays put so the pinned row doesn't jump mid-show.
      for (const tr of Array.from(tbody.rows)) {
        if (tr !== playingRow) tbody.removeChild(tr);
      }
    } else {
      tbody.textContent = "";
      // Reset the old offset before the new search's lines lay out, in the
      // frame: a scroll write forces a style update, which in a hidden window
      // leaves transitions Chrome never runs or frees.
      scrollResetPending = true;
    }
    rowMap.clear();
    maxDepth = 0;
    ranked = false;
    // Drop the width the removed lines grew the table to.
    fitTableToPvContent();
    fit();
  }

  // PV cell uses overflow:visible so long lines extend past the cell's
  // logical width. Grow the table to match so row borders and column
  // dividers extend to the right edge of the visible/scrollable content.
  function fitTableToPvContent() {
    let pvMax = 0;
    for (const row of tbody.rows) {
      const cell = cellOf(row, COL.PV);
      if (cell.scrollWidth > pvMax) pvMax = cell.scrollWidth;
    }
    tableEl.style.minWidth = headTableEl.style.minWidth = (header.fixedWidth() + pvMax) + "px";
  }
  // Coalesced: scrollWidth is a layout probe; per-info-event sync reads
  // would force a reflow on every engine info line.
  let scrollResetPending = false;
  const fit = rafCoalesce(() => {
    if (scrollResetPending) {
      scrollResetPending = false;
      scrollEl.scrollLeft = 0;
      scrollEl.scrollTop = 0;
    }
    fitTableToPvContent();
  });

  const rowKey = (depth, rank) => `${depth}:${rank}`;

  // Sort order: deeper first, then best line first within a depth.
  function sortsBefore(st, other) {
    return st.depth > other.depth || (st.depth === other.depth && st.rank < other.rank);
  }

  // First line below the best: rank the rows written before it.
  function showRanks() {
    ranked = true;
    for (const tr of tbody.rows) setRankBadge(tr, rowState.get(tr).rank);
  }

  function update(info, pvText) {
    const { depth, seldepth, score, nodes, nps, pv_uci } = info;
    if (depth == null) return;
    const rank = info.multipv ?? BEST_RANK;
    // A best-line depth 1 after maxDepth > 1 signals a new search; lines
    // below the best can trail a depth behind mid-search.
    if (depth === 1 && maxDepth > 1 && rank === BEST_RANK) clear();
    if (!ranked && rank !== BEST_RANK) showRanks();
    const key = rowKey(depth, rank);
    if (playingRow && rowMap.get(key) === playingRow) {
      // The playing row is frozen whole: infos without a PV are dropped;
      // one carrying a PV is a conflict -- unkey and pin, a fresh row
      // follows below.
      if (!pv_uci || !pv_uci.length) return;
      rowMap.delete(key);
    }
    if (depth > maxDepth) maxDepth = depth;
    let tr = rowMap.get(key);
    if (!tr) {
      tr = document.createElement("tr");
      const cells = new Map(COLUMNS.map((c) => [c.key, document.createElement("td")]));
      cells.get(COL.PV).className = PV_CELL_CLASS;
      const st = { depth, rank, cells };
      rowState.set(tr, st);
      layoutRow(tr);
      attachRowActivation(tr);
      rowMap.set(key, tr);
      let inserted = false;
      for (const row of tbody.rows) {
        if (sortsBefore(st, rowState.get(row))) {
          tbody.insertBefore(tr, row);
          inserted = true;
          break;
        }
      }
      if (!inserted) tbody.appendChild(tr);
    }
    cellOf(tr, COL.DEPTH).textContent = seldepth != null ? `${depth}/${seldepth}` : depth;
    if (score) cellOf(tr, COL.EVAL).textContent = fmtScore(score);
    if (nodes != null) cellOf(tr, COL.NODES).textContent = fmtCount(nodes);
    if (nps != null) cellOf(tr, COL.NPS).textContent = fmtCount(nps);
    if (pvText) renderPvInto(cellOf(tr, COL.PV), pvText);
    if (ranked) setRankBadge(tr, rank);
    // A row's {placement, pv_uci, frames} is written as one unit, and only by
    // an info that carries pv_uci -- infos without one update depth/score/
    // nodes/nps only, so the triple never disagrees with itself.
    if (pv_uci && pv_uci.length) {
      writePvRow(tr, currentPlacement?.(), pv_uci, pvText);
      applyShowability(tr);
    }
    fit();
  }

  function dispose() {
    clearPendingCancel();
    fit.cancel();
    header.destroy();
    cancelLine?.();
  }

  header.relayout();

  return { el, update, clear, fit, dispose, refreshShowability };
}
