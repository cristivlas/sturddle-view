// Drag-to-resize splitters for two-pane layouts. Adapted from the SPSA
// dashboard pattern: pointer-capture, snap-to-zero/full near the edges,
// ratio persisted in localStorage.
//
// The ratio is the fraction of the container occupied by the "before"
// pane (0..1). The caller writes the ratio into a CSS custom property
// on the container; the container's grid-template-{columns,rows} uses
// that custom property.

function clamp01(v) {
  return Math.max(0, Math.min(1, v));
}

// orientation: "horizontal" -> column splitter, drag on X.
//              "vertical"   -> row splitter, drag on Y.
// collapseThreshold: ratio below this fires onCollapse("before");
//   above (1-threshold) fires onCollapse("after"). Ratio resets to defaultRatio.
export function makeSplitter({
  handle, container, orientation, cssVar, storageKey,
  defaultRatio = 0.5,
  collapseThreshold = 0.02,
  onCollapse = null,
}) {
  function setRatio(r) {
    container.style.setProperty(cssVar, clamp01(r).toFixed(4));
  }

  const saved = parseFloat(localStorage.getItem(storageKey));
  const initial = Number.isFinite(saved) ? saved : defaultRatio;
  setRatio(initial);

  handle.addEventListener("pointerdown", (eDown) => {
    if (eDown.button !== 0) return;
    eDown.preventDefault();
    try { handle.setPointerCapture(eDown.pointerId); } catch { /* */ }
    handle.classList.add("dragging");

    const onMove = (e) => {
      const rect = container.getBoundingClientRect();
      const ratio = orientation === "horizontal"
        ? (e.clientX - rect.left) / rect.width
        : (e.clientY - rect.top) / rect.height;
      setRatio(ratio);
    };
    const onUp = () => {
      handle.classList.remove("dragging");
      const v = parseFloat(getComputedStyle(container).getPropertyValue(cssVar)) || 0;
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      if (onCollapse && v < collapseThreshold) {
        setRatio(defaultRatio);
        try { localStorage.setItem(storageKey, defaultRatio.toFixed(4)); } catch { /* */ }
        onCollapse("before");
        return;
      }
      if (onCollapse && v > 1 - collapseThreshold) {
        setRatio(defaultRatio);
        try { localStorage.setItem(storageKey, defaultRatio.toFixed(4)); } catch { /* */ }
        onCollapse("after");
        return;
      }
      try { localStorage.setItem(storageKey, v.toFixed(4)); } catch { /* */ }
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });
}
