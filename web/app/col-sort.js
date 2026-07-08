// Shared column-sort add-on for tables with a clickable <thead>. Peer of
// col-resize.js: the caller owns the data and DOM writes (via onSort), this
// helper owns header-click wiring, the cycle state machine, the sort-arrow
// indicator, aria-sort, and optional localStorage persistence.
//
// Model A (replace, not layered): one active column at a time. Clicking a
// header cycles that column's direction; clicking a different header switches
// to it. Ties are the caller's concern -- give each column a compare() that
// ends in a fixed tiebreaker so order is deterministic regardless of clicks.
//
// Coexists with col-resize: clicks that originate on a .th-grip (the resize
// handle) are ignored, so dragging a divider never triggers a sort.

import { loadJson, saveJson } from "./storage.js";

// Sort direction vocabulary, shared with sort-stack.js and table callers
// (column firstDir). NONE is the cleared/no-sort sentinel.
export const SORT_DIR = Object.freeze({ ASC: "asc", DESC: "desc", NONE: "none" });

export const ARROW_CLASS = "th-sort-arrow";
export const ARROW_DESC = "caret-down";
export const ARROW_ASC = "caret-up";

// Click cycle per column: none -> first -> other -> none. firstDir lets a
// column open ascending (names) or descending (dates/sizes).
export function nextDir(current, firstDir) {
  const other = firstDir === SORT_DIR.ASC ? SORT_DIR.DESC : SORT_DIR.ASC;
  if (current === firstDir) return other;
  if (current === other) return SORT_DIR.NONE;
  return firstDir;
}

export function attachColumnSort({
  table,
  columns,        // [{ key, firstDir?, sortable? }]; mapped to <thead> th by position (index), not by any attribute
  storageKey,     // optional; persists { key, dir }
  onSort,         // (state|null) => void; state = { key, dir, column }
}) {
  const ths = Array.from(table.querySelectorAll("thead th"));
  let active = null; // { key, dir }

  const saved = storageKey ? loadJson(storageKey) : null;
  if (saved && saved.key && (saved.dir === SORT_DIR.ASC || saved.dir === SORT_DIR.DESC)) {
    const col = columns.find((c) => c.key === saved.key && c.sortable !== false);
    if (col) active = { key: saved.key, dir: saved.dir };
  }

  function columnFor(key) {
    return columns.find((c) => c.key === key) || null;
  }

  function emit() {
    if (storageKey) saveJson(storageKey, active);
    onSort(active ? { ...active, column: columnFor(active.key) } : null);
  }

  function syncIndicators() {
    ths.forEach((th, i) => {
      const col = columns[i];
      th.querySelector(`.${ARROW_CLASS}`)?.remove();
      const dir = active && active.key === col?.key ? active.dir : SORT_DIR.NONE;
      th.setAttribute("aria-sort",
        dir === SORT_DIR.ASC ? "ascending" : dir === SORT_DIR.DESC ? "descending" : "none");
      if (dir === SORT_DIR.NONE) return;
      const arrow = document.createElement("wa-icon");
      arrow.className = ARROW_CLASS;
      arrow.setAttribute("name", dir === SORT_DIR.ASC ? ARROW_ASC : ARROW_DESC);
      th.append(arrow);
    });
  }

  ths.forEach((th, i) => {
    const col = columns[i];
    if (!col || col.sortable === false) return;
    th.classList.add("th-sortable");
    th.addEventListener("click", (ev) => {
      // Don't sort when the click was on the resize grip.
      if (ev.target.closest(".th-grip")) return;
      const firstDir = col.firstDir || SORT_DIR.ASC;
      const current = active && active.key === col.key ? active.dir : SORT_DIR.NONE;
      const dir = nextDir(current, firstDir);
      active = dir === SORT_DIR.NONE ? null : { key: col.key, dir };
      syncIndicators();
      emit();
    });
  });

  syncIndicators();

  return {
    // Caller invokes once after the first data load so the persisted sort is
    // applied to the initial render without a header click.
    current() {
      return active ? { ...active, column: columnFor(active.key) } : null;
    },
    destroy() {
      ths.forEach((th) => th.querySelector(`.${ARROW_CLASS}`)?.remove());
    },
  };
}
