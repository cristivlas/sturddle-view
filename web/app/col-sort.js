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

// Locale string compare shared by every table's name/text tiebreak and
// text-column primaries (case-insensitive, natural numeric order).
export function baseCompare(a, b) {
  return String(a).localeCompare(String(b), undefined, { sensitivity: "base", numeric: true });
}

// Model-A comparator factory. `primary` is the active column's dir-agnostic
// comparator; the result applies `dir` to it, then falls back to `tiebreak`
// (also dir-agnostic) so equal rows stay in a deterministic order. `group`,
// if given, orders before everything and is NOT dir-flipped -- for fixed
// partitions like folders-before-files.
export function modelACompare({ dir, primary, tiebreak, group }) {
  const sign = dir === SORT_DIR.ASC ? 1 : -1;
  return (a, b) => {
    if (group) { const g = group(a, b); if (g) return g; }
    const p = primary(a, b);
    return p !== 0 ? sign * p : tiebreak(a, b);
  };
}

// Nearest scrollable ancestor; null when everything fits (nothing to scroll).
function scrollParent(el) {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const o = getComputedStyle(p).overflowY;
    if ((o === "auto" || o === "scroll") && p.scrollHeight > p.clientHeight) return p;
  }
  return null;
}

// Bring `row` into view -- FULLY visible. scrollIntoView({block:"nearest"})
// aligns to the scroller's edges, which leaves the row obscured under a sticky
// <thead> at the top or under a bottom overlay (search bar) whose height the
// caller reserved as scroller padding-bottom; treat both as viewport insets.
export function scrollRowIntoView(row) {
  const scroller = row && scrollParent(row);
  if (!scroller) return;
  const rowRect = row.getBoundingClientRect();
  const scRect = scroller.getBoundingClientRect();
  const thead = row.closest("table")?.querySelector("thead");
  const top = scRect.top + (thead?.getBoundingClientRect().height || 0);
  const bottom = scRect.bottom - (parseFloat(getComputedStyle(scroller).paddingBottom) || 0);
  if (rowRect.top < top) scroller.scrollTop -= top - rowRect.top;
  else if (rowRect.bottom > bottom) scroller.scrollTop += rowRect.bottom - bottom;
}

// Same, for the row a sort-triggered re-render left selected.
export function scrollSortedRowIntoView(root, selector) {
  scrollRowIntoView(root.querySelector(selector));
}

// Click cycle per column: none -> first -> other -> none. firstDir lets a
// column open ascending (names) or descending (dates/sizes).
export function nextDir(current, firstDir) {
  const other = firstDir === SORT_DIR.ASC ? SORT_DIR.DESC : SORT_DIR.ASC;
  if (current === firstDir) return other;
  if (current === other) return SORT_DIR.NONE;
  return firstDir;
}

// Companion controller for a pair of direction buttons (asc / desc) that sort
// one fixed column, sharing the same { key, dir } state that attachColumnSort
// drives. Both controllers read/write via the caller's get/set, so a header
// click lights the matching button and vice versa -- one state, two UIs.
// set(state|null) is the caller's apply+persist+re-render; get() returns the
// live state. Returns { sync } to re-light the buttons after external changes.
export function attachButtonSort({
  ascBtn,
  descBtn,
  key,
  activeClass = "is-active",
  get,
  set,
}) {
  function sync() {
    const s = get();
    const dir = s && s.key === key ? s.dir : SORT_DIR.NONE;
    ascBtn.classList.toggle(activeClass, dir === SORT_DIR.ASC);
    descBtn.classList.toggle(activeClass, dir === SORT_DIR.DESC);
  }

  function click(dir) {
    const s = get();
    const active = s && s.key === key && s.dir === dir;
    set(active ? null : { key, dir });
    sync();
  }

  ascBtn.addEventListener("click", () => click(SORT_DIR.ASC));
  descBtn.addEventListener("click", () => click(SORT_DIR.DESC));
  sync();
  return { sync };
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
    // Programmatic sort (e.g. a companion button pair), routed through the same
    // arrow-sync + persist + onSort path a header click takes.
    set(state) {
      active = state && state.key ? { key: state.key, dir: state.dir } : null;
      syncIndicators();
      emit();
    },
    destroy() {
      ths.forEach((th) => th.querySelector(`.${ARROW_CLASS}`)?.remove());
    },
  };
}
