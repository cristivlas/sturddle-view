// Pure layout: row-major slot grid anchored at (left, top).
//
// Hands out fixed-size cell rects so live-board windows align. Slots
// are picked by current occupancy: a slot is "free" if no live window
// overlaps its rect. That way, dragging a window out of its slot makes
// the slot reusable without any explicit release call.

export const SLOT_GAP = 1;

export function createSlotGrid({ top, left, getCellWidth, cellHeight, gap = SLOT_GAP, getWindows, getRight = () => window.innerWidth, getMaxRows = () => Infinity }) {
  function gridDims() {
    const cw = getCellWidth();
    const availW = Math.max(0, getRight() - left);
    const availH = Math.max(0, window.innerHeight - top);
    const cols = Math.max(1, Math.floor((availW + gap) / (cw + gap)));
    const rows = Math.min(getMaxRows(), Math.max(1, Math.floor((availH + gap) / (cellHeight + gap))));
    return { cols, rows, cw };
  }

  function rectAt(slot) {
    const { cols, cw } = gridDims();
    const col = slot % cols;
    const row = Math.floor(slot / cols);
    return {
      x: left + col * (cw + gap),
      y: top + row * (cellHeight + gap),
      w: cw,
      h: cellHeight,
    };
  }

  function capacity() {
    const { cols, rows } = gridDims();
    return cols * rows;
  }

  function rectsOverlap(a, b) {
    return !(a.x + a.w <= b.x || b.x + b.w <= a.x ||
             a.y + a.h <= b.y || b.y + b.h <= a.y);
  }

  function slotIsOccupied(slot, exclude) {
    const r = rectAt(slot);
    for (const wb of getWindows()) {
      if (wb.min || wb === exclude) continue;
      const wr = { x: wb.x, y: wb.y, w: wb.width, h: wb.height };
      if (rectsOverlap(r, wr)) return true;
    }
    return false;
  }

  // Returns the lowest unoccupied slot's rect, or null if all are taken.
  function claim(exclude) {
    const cap = capacity();
    for (let i = 0; i < cap; i++) {
      if (!slotIsOccupied(i, exclude)) return rectAt(i);
    }
    return null;
  }

  return { claim, capacity, rectAt };
}
