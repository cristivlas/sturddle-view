// Layered (MRU) sort companion to col-sort.js. attachColumnSort emits a single
// active column (Model A); attachLayeredSort turns that stream into a
// most-recent-first stack so every earlier sort survives as a deeper tiebreak
// (Model B). The sorter is key-agnostic: comparison semantics live on each
// column descriptor (field / numeric / tiebreak), so callers declare columns
// once and never write a per-table compare switch.

import { loadJson, saveJson } from "./storage.js";
import { attachColumnSort, SORT_DIR } from "./col-sort.js";

// Dir-agnostic comparator derived from a column descriptor and memoized on it.
// `field` names the data property (defaults to the column key). A `rank` map
// orders by meaning (value -> rank; unknown/missing sort last); numeric columns
// subtract; the rest compare case-insensitively as text.
// NOTE: numeric coerces missing/non-numeric to 0 -- fine where 0 isn't a real
// value (e.g. the 1-based num); revisit if numeric ever wraps such a column.
function columnCmp(col) {
  if (col._cmp) return col._cmp;
  const field = col.field || col.key;
  if (col.rank) {
    const rankOf = (v) => col.rank[v] ?? Number.MAX_SAFE_INTEGER;
    col._cmp = (a, b) => rankOf(a[field]) - rankOf(b[field]);
  } else if (col.numeric) {
    col._cmp = (a, b) => (Number(a[field]) || 0) - (Number(b[field]) || 0);
  } else {
    col._cmp = (a, b) => (a[field] || "").localeCompare(b[field] || "", undefined, { sensitivity: "base" });
  }
  return col._cmp;
}

// MRU stack of { key, dir }, most-recent first. Each header click promotes its
// column to the front; a null state (col-sort's 3rd click) clears the stack.
function promoteSort(stack, state) {
  if (!state) return [];
  const s = { key: state.key, dir: state.dir };
  return [s, ...stack.filter((e) => e.key !== s.key)];
}

// Wire layered sorting onto a table: attachColumnSort drives the header
// arrow/cycle and persists its single top entry (sortKey); the full stack is
// kept under stackKey, restored on init (falling back to that top entry), and
// repromoted on each click. Returns the live stack via get() for the renderer.
// `defaultState` ({ key, dir }) applies on first-ever visit only: a persisted
// stack -- including the empty one a clearing third click leaves -- wins.
export function attachLayeredSort({ table, columns, sortKey, stackKey, onChange, defaultState = null }) {
  let stack = [];
  const sortCtrl = attachColumnSort({
    table, columns, storageKey: sortKey,
    onSort: (state) => {
      stack = promoteSort(stack, state);
      saveJson(stackKey, stack);
      onChange();
    },
  });
  const saved = loadJson(stackKey);
  const cur = sortCtrl.current();
  if (Array.isArray(saved) && saved.length) stack = saved;
  else if (cur) stack = [{ key: cur.key, dir: cur.dir }];
  else if (defaultState && !Array.isArray(saved)) {
    // set() routes through the arrow-sync + persist + onSort path, so the
    // header indicator matches the seeded stack.
    sortCtrl.set(defaultState);
  }
  return { get: () => stack };
}

// Stable in-place sort: walk the MRU stack, first differing column wins; the
// column flagged `tiebreak` resolves rows equal on every stacked column. Empty
// stack leaves the rows untouched (natural order). The column->cmp plan is
// resolved up front so the comparator hot path carries no lookup.
export function sortByStack(rows, stack, columns) {
  if (!stack.length) return;
  const plan = [];
  for (const s of stack) {
    const col = columns.find((c) => c.key === s.key);
    if (col) plan.push({ cmp: columnCmp(col), dir: s.dir === SORT_DIR.DESC ? -1 : 1 });
  }
  // Optional final tiebreak; absent -> rows equal on every stacked column keep
  // their input order.
  const tieCol = columns.find((c) => c.tiebreak);
  const tie = tieCol ? columnCmp(tieCol) : () => 0;
  rows.sort((a, b) => {
    for (const p of plan) { const c = p.dir * p.cmp(a, b); if (c) return c; }
    return tie(a, b);
  });
}
