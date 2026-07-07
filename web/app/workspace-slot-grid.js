// Pure layout: row-major slot grid anchored at (left, top).
//
// Hands out fixed-size cell rects so live-board windows align. Slots
// are picked by current occupancy: a slot is "free" if no live window
// overlaps its rect. That way, dragging a window out of its slot makes
// the slot reusable without any explicit release call.

export const SLOT_GAP = 1;

export function createSlotGrid({ top, left, getCellWidth, cellHeight, gap = SLOT_GAP, getWindows, getRight = () => window.innerWidth, getBottom = () => window.innerHeight, getMaxRows = () => Infinity }) {
  function gridDims() {
    const cw = getCellWidth();
    const availW = Math.max(0, getRight() - left);
    const availH = Math.max(0, getBottom() - top);
    const cols = Math.max(1, Math.floor((availW + gap) / (cw + gap)));
    const rows = Math.min(getMaxRows(), Math.max(1, Math.floor((availH + gap) / (cellHeight + gap))));
    return { cols, rows, cw };
  }

  function rectAt(slot) {
    const { cols, cw } = gridDims();
    const col = slot % cols;
    const row = Math.floor(slot / cols);
    // Last column absorbs the floor remainder so the row's right edge reaches
    // getRight() exactly (no dead strip); other columns stay uniform width.
    const availW = Math.max(0, getRight() - left);
    const rowRightEdge = left + cols * cw + (cols - 1) * gap;
    const extra = col === cols - 1 ? (left + availW) - rowRightEdge : 0;
    return {
      x: left + col * (cw + gap),
      y: top + row * (cellHeight + gap),
      w: cw + extra,
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

  function wbRect(wb) {
    return { x: wb.x, y: wb.y, w: wb.width, h: wb.height };
  }

  function slotIsOccupied(slot, exclude) {
    const r = rectAt(slot);
    for (const wb of getWindows()) {
      if (wb.min || wb === exclude) continue;
      if (rectsOverlap(r, wbRect(wb))) return true;
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

  // True if wb's rect overlaps any slot cell -- i.e. it currently holds a
  // grid slot rather than floating dragged-out somewhere off the grid.
  function occupiesSlot(wb) {
    if (wb.min) return false;
    const wr = wbRect(wb);
    const cap = capacity();
    for (let i = 0; i < cap; i++) {
      if (rectsOverlap(rectAt(i), wr)) return true;
    }
    return false;
  }

  return { claim, capacity, rectAt, occupiesSlot };
}
