// Shared column-resize helper for tables with .th-grip handles.
// Owns pointer capture, drag-line overlays, localStorage load/save, and
// cleanup. The caller owns units (% vs px), clamping, and DOM writes via
// the applySizes callback.

import { loadJson, saveJson } from "./storage.js";

const DRAG_LINE_CLASS = "col-drag-line";

// Horizontal travel (px) a touch must cover before a drag starts. Below it,
// the gesture stays a tap (click passes through) or a vertical pan
// (touch-action: pan-y lets the browser scroll).
const TOUCH_DRAG_SLOP_PX = 6;

export function attachColumnResize({
  table,
  grips,
  overlayHost,
  storageKey,
  sizes,           // caller-owned array; helper mutates in place
  unit,            // "pct" | "px" -- only used to position drag-lines
  applySizes,
  dragLineHeight,  // optional () => number; defaults to overlayHost.scrollHeight
  onSave = null,   // optional (sizes) => void; fires after drag-end persist
}) {
  const persistArity = sizes.length;

  const saved = loadJson(storageKey);
  if (Array.isArray(saved) && saved.length === persistArity) {
    for (let i = 0; i < persistArity; i++) sizes[i] = saved[i];
  }
  applySizes(sizes);

  const cleanups = [];
  grips.forEach((grip, gripIdx) => {
    let justDragged = false;

    function onDown(eDown) {
      if (eDown.button !== 0) return;
      justDragged = false;
      const touch = eDown.pointerType !== "mouse";
      let startX = eDown.clientX;
      let dragging = false;
      let startSizes, tableW, rightLine, leftLine;

      function placeLines(boundaryX) {
        const hostLeft = overlayHost.getBoundingClientRect().left;
        const thLeft = table.querySelectorAll("thead th")[gripIdx].getBoundingClientRect().left;
        rightLine.style.left = (boundaryX - hostLeft) + "px";
        const fullH = dragLineHeight ? dragLineHeight() : overlayHost.scrollHeight;
        rightLine.style.height = leftLine.style.height = fullH + "px";
        leftLine.style.left = (thLeft - hostLeft) + "px";
      }

      function startDrag(fromX) {
        dragging = true;
        startX = fromX;
        grip.setPointerCapture(eDown.pointerId);
        grip.classList.add("dragging");
        startSizes = sizes.slice();
        tableW = table.getBoundingClientRect().width || 1;
        rightLine = document.createElement("div");
        leftLine = document.createElement("div");
        rightLine.className = leftLine.className = DRAG_LINE_CLASS;
        rightLine.style.top = leftLine.style.top = "0";
        overlayHost.appendChild(rightLine);
        overlayHost.appendChild(leftLine);
        placeLines(fromX);
      }

      // Mouse drags start immediately; a touch waits for horizontal slop so
      // taps fall through to sort and vertical pans scroll (touch-action:
      // pan-y hands those to the browser, which fires pointercancel).
      if (!touch) {
        eDown.preventDefault();
        startDrag(eDown.clientX);
      }

      function onMove(e) {
        if (!dragging) {
          if (Math.abs(e.clientX - startX) < TOUCH_DRAG_SLOP_PX) return;
          startDrag(e.clientX);
        }
        const deltaFrac = (e.clientX - startX) / tableW;
        applySizes(sizes, { deltaFrac, tableWidth: tableW, startSizes, gripIdx });
        // Boundary derived from current (clamped) sizes so the line stops
        // at the column edge instead of running off with the cursor.
        const tableLeft = table.getBoundingClientRect().left;
        let boundary = 0;
        for (let i = 0; i <= gripIdx; i++) boundary += sizeToPx(sizes[i], tableW);
        placeLines(tableLeft + boundary);
      }
      let done = false;
      function onUp(e) {
        if (done) return;
        done = true;
        grip.removeEventListener("pointermove", onMove);
        grip.removeEventListener("pointerup", onUp);
        grip.removeEventListener("pointercancel", onUp);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onUp);
        if (!dragging) return;
        // A real drag's trailing click must not be forwarded as a tap.
        justDragged = e.type === "pointerup";
        grip.classList.remove("dragging");
        rightLine.remove();
        leftLine.remove();
        saveJson(storageKey, sizes.slice(0, persistArity));
        if (onSave) onSave(sizes.slice(0, persistArity));
      }
      grip.addEventListener("pointermove", onMove);
      grip.addEventListener("pointerup", onUp);
      grip.addEventListener("pointercancel", onUp);
      document.addEventListener("pointerup", onUp);
      document.addEventListener("pointercancel", onUp);
    }

    // A tap on the grip (touch, no drag) forwards its click to whatever the
    // finger was over, so wide coarse-pointer grips don't dead-zone header
    // sorting. isTrusted guards recursion through the forwarded click.
    function onClick(e) {
      if (!e.isTrusted) return;
      e.preventDefault();
      e.stopPropagation();
      if (justDragged) {
        justDragged = false;
        return;
      }
      grip.style.pointerEvents = "none";
      const under = document.elementFromPoint(e.clientX, e.clientY);
      grip.style.pointerEvents = "";
      if (under) under.click();
    }

    grip.addEventListener("pointerdown", onDown);
    grip.addEventListener("click", onClick);
    cleanups.push(() => {
      grip.removeEventListener("pointerdown", onDown);
      grip.removeEventListener("click", onClick);
    });
  });

  function sizeToPx(v, tableW) {
    return unit === "pct" ? (v / 100) * tableW : v;
  }

  return {
    destroy() {
      cleanups.forEach((fn) => fn());
    },
  };
}

// Build the `applySizes` callback for percentage-width columns: on a drag it
// clamps the dragged boundary to `minPct` (stealing the deficit from the
// neighbor), then writes every column's width. Shared by the standings,
// tourney, and head-to-head tables so the clamp math lives in one place.
export function makePctApplySizes(colEls, minPct) {
  return function applySizes(sizes, rctx) {
    if (rctx) {
      const { deltaFrac, startSizes, gripIdx } = rctx;
      const dPct = deltaFrac * 100;
      let a = startSizes[gripIdx] + dPct;
      let b = startSizes[gripIdx + 1] - dPct;
      if (a < minPct) { b -= minPct - a; a = minPct; }
      if (b < minPct) { a -= minPct - b; b = minPct; }
      sizes[gripIdx] = a;
      sizes[gripIdx + 1] = b;
    }
    colEls.forEach((c, i) => { c.style.width = sizes[i] + "%"; });
  };
}
