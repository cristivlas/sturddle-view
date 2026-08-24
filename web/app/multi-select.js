// Row multi-selection shared by list/table views: Shift+click ranges,
// Ctrl/Cmd+click toggles, plain click collapses to a single row.
//
// The tournament views bind their panes (workspace, standings, event log) to
// one "anchor" row -- the view's own selection. The anchor is read-only here:
// modifier gestures never move it and never unmark it, so a range always
// starts at the anchor and the marked set is empty while the anchor alone is
// selected.

const MARKED_CLASS = "marked";

// Shift+Arrow extends exactly like Shift+click, so it replays the gesture.
const SHIFT_GESTURE = { shiftKey: true };

// Mutate `marked` / `anchorRef` for one click gesture. `idsInOrder` returns
// the ids as displayed, which is what a Shift range walks.
export function applyListClick(ev, id, marked, anchorRef, idsInOrder) {
  if (ev.shiftKey && anchorRef.id != null) {
    // Range select from anchor to id (inclusive), in display order.
    const ids = idsInOrder();
    const a = ids.indexOf(anchorRef.id);
    const b = ids.indexOf(id);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      marked.clear();
      for (let i = lo; i <= hi; i++) marked.add(ids[i]);
    }
  } else if (ev.ctrlKey || ev.metaKey) {
    // Toggle this row in/out; keep anchor on the toggled row.
    if (marked.has(id)) marked.delete(id);
    else marked.add(id);
    anchorRef.id = id;
  } else {
    // Plain click: collapse to just this row.
    marked.clear();
    marked.add(id);
    anchorRef.id = id;
  }
}

// getRows: live row elements in display order (each carries data-id).
// getAnchorId: the view's single selection. selectOne: bind the view to a row.
// onChange: called after a gesture changed the marked set.
export function createMultiSelect({ getRows, getAnchorId, selectOne, onChange }) {
  const marked = new Set();
  let leadId = null;

  const rowIds = () => Array.from(getRows(), (row) => row.dataset.id);

  // The anchor paints itself as the view's selection, so only the extra rows
  // carry the marked class.
  const paint = () => {
    const anchor = getAnchorId();
    for (const row of getRows()) {
      const id = row.dataset.id;
      row.classList.toggle(MARKED_CLASS, id !== anchor && marked.has(id));
    }
  };

  // The anchor is always part of a multi-selection; a set that shrank back to
  // it alone is a plain selection again.
  const normalize = () => {
    const anchor = getAnchorId();
    if (anchor) marked.add(anchor);
    if (marked.size <= 1) marked.clear();
  };

  const collapseTo = (id) => {
    marked.clear();
    leadId = id;
    paint();
  };

  const gesture = (ev, id) => {
    leadId = id;
    const anchor = getAnchorId();
    if (!marked.size && anchor) marked.add(anchor);
    applyListClick(ev, id, marked, { id: anchor }, rowIds);
    normalize();
    paint();
    onChange?.();
  };

  return {
    multi: () => marked.size > 1,
    ids: () => rowIds().filter((id) => marked.has(id)),
    // Row the arrow keys move from: the last one a gesture touched.
    lead: () => leadId ?? getAnchorId(),
    collapseTo,
    extendTo: (id) => gesture(SHIFT_GESTURE, id),
    handleClick(ev, id) {
      if (ev.shiftKey || ev.ctrlKey || ev.metaKey) {
        gesture(ev, id);
        return;
      }
      collapseTo(id);
      selectOne(id);
    },
    // After a re-render: forget ids whose rows are gone, then repaint.
    prune() {
      const valid = new Set(rowIds());
      for (const id of marked) if (!valid.has(id)) marked.delete(id);
      if (leadId && !valid.has(leadId)) leadId = null;
      normalize();
      paint();
    },
  };
}

// Items a ribbon verb acts on: the marked set in display order while
// multi-selecting, else the anchor item alone.
export function selectionTargets(ms, items, anchorItem) {
  if (!ms.multi()) return anchorItem ? [anchorItem] : [];
  const byId = new Map(items.map((it) => [it.id, it]));
  return ms.ids().map((id) => byId.get(id)).filter(Boolean);
}
