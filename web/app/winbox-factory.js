// The app's one way to create WinBox windows (createWinBox), so fixes to
// WinBox behavior apply to every window.
//
// Drag-from-maximized fix: dragging a maximized WinBox restores it at the
// left, centre or right of its area (by which third the pointer is in),
// ignoring where the title bar was grabbed, so a narrow window can land away
// from the cursor. Instead, on the first drag move this restores the window
// itself with the pointer at the same relative spot along the title bar's
// drag span (never over its control buttons); WinBox's own drag then carries
// on with an ordinary window.
//
// Ordering: WinBox listens for the press on the title bar and for moves on
// window, both in the capture phase. Our press listener sits on the window
// element (an ancestor, capture), so it runs first and registers our move
// listener ahead of WinBox's.

const DRAG_HANDLE_SEL = ".wb-drag";
const HEADER_SEL = ".wb-header";
const CONTROLS_SEL = ".wb-control";
const NO_MOVE_CLASS = "no-move";
// [press, move, release] event names per input kind.
const GESTURES = [
  ["mousedown", "mousemove", "mouseup"],
  ["touchstart", "touchmove", "touchend"],
];
const CAPTURE = { capture: true };

const pageX = (ev) => (ev.touches ? ev.touches[0] : ev).pageX;

// The title bar's actual drag span in page x: the header minus its
// right-floated controls (.wb-drag spans the whole header, under them).
function dragSpan(wb) {
  const h = wb.g.querySelector(HEADER_SEL).getBoundingClientRect();
  const c = wb.g.querySelector(CONTROLS_SEL)?.getBoundingClientRect();
  const right = c?.width ? c.left : h.right;
  return { left: h.left + window.scrollX, width: Math.max(1, right - h.left) };
}

function keepPointerOnMaxDrag(wb) {
  for (const [press, move, release] of GESTURES) {
    wb.g.addEventListener(press, (ev) => {
      if (!wb.max || wb.g.classList.contains(NO_MOVE_CLASS) || !ev.target.closest(DRAG_HANDLE_SEL)) return;
      const x0 = pageX(ev);
      const span = dragSpan(wb);
      const frac = (x0 - span.left) / span.width;
      const top = wb.g.getBoundingClientRect().top + window.scrollY;
      const stop = () => {
        window.removeEventListener(move, onMove, CAPTURE);
        window.removeEventListener(release, stop, CAPTURE);
      };
      function onMove() {
        stop();
        // A double-click restore (WinBox's own) leaves nothing to do.
        if (!wb.max) return;
        // Tells onrestore handlers this restore is a drag's, not a click's.
        wb.svDragRestoring = true;
        try { wb.restore(); } finally { wb.svDragRestoring = false; }
        // Place the restored window so the press point sits at the same
        // fraction of its (narrower) drag span; WinBox's move handler then
        // adds the pointer travel since the press.
        const restored = dragSpan(wb);
        const spanOffset = restored.left - (wb.g.getBoundingClientRect().left + window.scrollX);
        wb.move(x0 - spanOffset - frac * restored.width, top);
      }
      window.addEventListener(move, onMove, CAPTURE);
      window.addEventListener(release, stop, CAPTURE);
    }, CAPTURE);
  }
}

// Bottom fence fix: WinBox reads one `bottom` inset both as maximize's bottom
// edge and as the line minimized bars stack on. The caller's `bottom` fences
// maximize only; minimized bars dock at the viewport bottom, in the strip the
// layouts reserve (--wb-min-footer-h). Swapped on the way into each state.
function fenceMaximizeOnly(wb, bottom) {
  const { minimize, maximize } = wb;
  wb.minimize = function (state) { this.bottom = 0; return minimize.call(this, state); };
  wb.maximize = function (state) { this.bottom = bottom; return maximize.call(this, state); };
}

// `new WinBox(opts)` plus the app's behavior fixes. WinBox is the global the
// vendored script defines.
export function createWinBox(opts) {
  // Born minimized: dock in the strip from the start (the wrappers below
  // aren't installed until after construction).
  const wb = new WinBox(opts.bottom && opts.min ? { ...opts, bottom: 0 } : opts);
  keepPointerOnMaxDrag(wb);
  if (opts.bottom) fenceMaximizeOnly(wb, opts.bottom);
  return wb;
}
