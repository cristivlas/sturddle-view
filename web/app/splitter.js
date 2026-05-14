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
//
// minBeforePx / minAfterPx: minimum pixels each pane retains. Pane can
// shrink to this stub but never below, so the splitter handle stays
// reachable (no full-collapse).
export function makeSplitter({
  handle, container, orientation, cssVar, storageKey,
  defaultRatio = 0.5,
  minBeforePx = 1,
  minAfterPx = 1,
}) {
  function clampRatio(r, sizePx) {
    r = clamp01(r);
    if (sizePx > 0) {
      const minR = minBeforePx / sizePx;
      const maxR = 1 - (minAfterPx / sizePx);
      // If sizePx is so small that minR > maxR, fall back to mid.
      if (minR > maxR) return 0.5;
      r = Math.max(minR, Math.min(maxR, r));
    }
    return r;
  }
  function setRatio(r, sizePx = 0) {
    container.style.setProperty(cssVar, clampRatio(r, sizePx).toFixed(4));
  }

  // Initial value from storage, fall back to default.
  const saved = parseFloat(localStorage.getItem(storageKey));
  const initial = Number.isFinite(saved) ? saved : defaultRatio;
  setRatio(initial);
  // Re-clamp once the container has been laid out so a previously-saved
  // collapse ratio respects the min-px floor under the new sizing rules.
  requestAnimationFrame(() => {
    const rect = container.getBoundingClientRect();
    const sizePx = orientation === "horizontal" ? rect.width : rect.height;
    setRatio(initial, sizePx);
  });

  handle.addEventListener("pointerdown", (eDown) => {
    if (eDown.button !== 0) return;
    eDown.preventDefault();
    try { handle.setPointerCapture(eDown.pointerId); } catch { /* */ }
    handle.classList.add("dragging");

    const onMove = (e) => {
      const rect = container.getBoundingClientRect();
      const sizePx = orientation === "horizontal" ? rect.width : rect.height;
      const ratio = orientation === "horizontal"
        ? (e.clientX - rect.left) / rect.width
        : (e.clientY - rect.top) / rect.height;
      setRatio(ratio, sizePx);
    };
    const onUp = () => {
      handle.classList.remove("dragging");
      const v = parseFloat(getComputedStyle(container).getPropertyValue(cssVar)) || 0;
      try { localStorage.setItem(storageKey, v.toFixed(4)); } catch { /* */ }
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });
}
