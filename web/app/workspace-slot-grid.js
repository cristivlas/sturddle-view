// Pure layout: row-major slot grid anchored at (left, top).
//
// Hands out fixed-size cell rects so live-board windows align. Slots
// are picked by current occupancy: a slot is "free" if no live window
// overlaps its rect. That way, dragging a window out of its slot makes
// the slot reusable without any explicit release call.

const SLOT_GAP = 8;

export function createSlotGrid({ top, left, cellWidth, cellHeight, gap = SLOT_GAP, getWindows }) {
  function gridDims() {
    const availW = Math.max(0, window.innerWidth - left);
    const availH = Math.max(0, window.innerHeight - top);
    const cols = Math.max(1, Math.floor((availW + gap) / (cellWidth + gap)));
    const rows = Math.max(1, Math.floor((availH + gap) / (cellHeight + gap)));
    return { cols, rows };
  }

  function rectAt(slot) {
    const { cols } = gridDims();
    const col = slot % cols;
    const row = Math.floor(slot / cols);
    return {
      x: left + col * (cellWidth + gap),
      y: top + row * (cellHeight + gap),
      w: cellWidth,
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

  function slotIsOccupied(slot) {
    const r = rectAt(slot);
    for (const wb of getWindows()) {
      if (wb.min) continue;
      const wr = { x: wb.x, y: wb.y, w: wb.width, h: wb.height };
      if (rectsOverlap(r, wr)) return true;
    }
    return false;
  }

  // Returns the lowest unoccupied slot's rect, or null if all are taken.
  function claim() {
    const cap = capacity();
    for (let i = 0; i < cap; i++) {
      if (!slotIsOccupied(i)) return { slot: i, ...rectAt(i) };
    }
    return null;
  }

  return { claim, capacity, rectAt };
}
