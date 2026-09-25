// Shared plumbing for the head/body split-table pattern: a fixed header
// table above a separately-scrolling body table (own colgroup each, same
// widths) so the body's scrollbar track starts below the header instead of
// a sticky thead sharing the body's scrollbar. Peer of col-resize.js.

// Keeps a fixed header's horizontal scroll in lockstep with its scrolling
// body counterpart (PV/path overflow, etc).
export function wireSplitScroll(headScroll, bodyScroll) {
  bodyScroll.addEventListener("scroll", () => {
    headScroll.scrollLeft = bodyScroll.scrollLeft;
  });
}

// Column-width writer for a split table: applies `sizes` to every colgroup
// in `colElsList` (head first, then body, ...) so resize/sort keep every
// table's columns aligned. Pass as (or wrap into) attachColumnResize's
// applySizes.
export function applySplitColWidths(colElsList, sizes, unit = "%") {
  for (const colEls of colElsList) {
    colEls.forEach((c, i) => { c.style.width = sizes[i] + unit; });
  }
}
