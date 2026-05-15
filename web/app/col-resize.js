// Shared column-resize helper for tables with .th-grip handles.
// Owns pointer capture, drag-line overlays, localStorage load/save, and
// cleanup. The caller owns units (% vs px), clamping, and DOM writes via
// the applySizes callback.

const DRAG_LINE_CLASS = "col-drag-line";

export function attachColumnResize({
  table,
  grips,
  overlayHost,
  storageKey,
  sizes,           // caller-owned array; helper mutates in place
  unit,            // "pct" | "px" -- only used to position drag-lines
  applySizes,
  dragLineHeight,  // optional () => number; defaults to overlayHost.scrollHeight
}) {
  const persistArity = sizes.length;

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (Array.isArray(saved) && saved.length === persistArity) {
      for (let i = 0; i < persistArity; i++) sizes[i] = saved[i];
    }
  } catch (e) { /* use defaults */ }
  applySizes(sizes);

  const cleanups = [];
  grips.forEach((grip, gripIdx) => {
    function onDown(eDown) {
      if (eDown.button !== 0) return;
      eDown.preventDefault();
      grip.setPointerCapture(eDown.pointerId);
      grip.classList.add("dragging");
      const startX = eDown.clientX;
      const startSizes = sizes.slice();
      const tableW = table.getBoundingClientRect().width || 1;

      const rightLine = document.createElement("div");
      const leftLine = document.createElement("div");
      rightLine.className = leftLine.className = DRAG_LINE_CLASS;
      rightLine.style.top = leftLine.style.top = "0";
      overlayHost.appendChild(rightLine);
      overlayHost.appendChild(leftLine);

      function placeLines(boundaryX) {
        const hostLeft = overlayHost.getBoundingClientRect().left;
        const thLeft = table.querySelectorAll("thead th")[gripIdx].getBoundingClientRect().left;
        rightLine.style.left = (boundaryX - hostLeft) + "px";
        const fullH = dragLineHeight ? dragLineHeight() : overlayHost.scrollHeight;
        rightLine.style.height = leftLine.style.height = fullH + "px";
        leftLine.style.left = (thLeft - hostLeft) + "px";
      }
      placeLines(eDown.clientX);

      function onMove(e) {
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
      function onUp() {
        if (done) return;
        done = true;
        grip.classList.remove("dragging");
        rightLine.remove();
        leftLine.remove();
        localStorage.setItem(storageKey, JSON.stringify(sizes.slice(0, persistArity)));
        grip.removeEventListener("pointermove", onMove);
        grip.removeEventListener("pointerup", onUp);
        grip.removeEventListener("pointercancel", onUp);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onUp);
      }
      grip.addEventListener("pointermove", onMove);
      grip.addEventListener("pointerup", onUp);
      grip.addEventListener("pointercancel", onUp);
      document.addEventListener("pointerup", onUp);
      document.addEventListener("pointercancel", onUp);
    }
    grip.addEventListener("pointerdown", onDown);
    cleanups.push(() => grip.removeEventListener("pointerdown", onDown));
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
