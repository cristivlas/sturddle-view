// Floating ribbon: wraps the active ribbon element (.board-ribbon or
// .tournaments-ribbon) in a WinBox. Horizontal layout, no resize handles.
// Position persisted to localStorage. On user-close, fires
// sturddle:ribbon-float-closed so main.js can revert to edge-docked.

// LocalStorage keys: side selection ("left"|"right"|"float") and the
// WinBox geometry for the floating mode.
export const RIBBON_SIDE_KEY = "sturddle:ribbon:side";
const GEO_KEY = "sturddle:ribbon:geo";
const HEADER_H = 44; // px -- nav header height (top boundary)

let wb = null;
let currentEl = null;
let originalParent = null; // where currentEl lived before we moved it
let originalNextSibling = null; // sibling to insertBefore on restore
let programmaticClose = false;

function loadGeo() {
  try { return JSON.parse(localStorage.getItem(GEO_KEY)) || null; } catch { return null; }
}

function saveGeo() {
  if (!wb) return;
  localStorage.setItem(GEO_KEY, JSON.stringify({ x: wb.x, y: wb.y }));
}

export function isRibbonFloating() {
  return wb !== null;
}

function restoreElementToOrigin() {
  if (!currentEl || !originalParent) return;
  clearInlineStyles(currentEl);
  currentEl.style.display = ""; // play.js refreshButtons sets the right value
  if (originalNextSibling && originalNextSibling.parentNode === originalParent) {
    originalParent.insertBefore(currentEl, originalNextSibling);
  } else {
    originalParent.appendChild(currentEl);
  }
}

function rememberOrigin(el) {
  // Only record if the element is outside the WinBox -- otherwise we'd
  // record wb.body as the "origin" and destroy the ribbon on close.
  if (wb && wb.body && wb.body.contains(el)) return;
  originalParent = el.parentNode;
  originalNextSibling = el.nextSibling;
}

function mountEl(el) {
  if (!el || !wb) return;
  // Already mounted -- no-op. Avoids layout thrash on repeated refreshButtons.
  if (currentEl === el && wb.body.contains(el)) return;
  // If swapping ribbons, restore the previous one to its origin first.
  if (currentEl && currentEl !== el) {
    restoreElementToOrigin();
  }
  rememberOrigin(el);
  currentEl = el;
  wb.body.innerHTML = "";
  wb.body.appendChild(el);
  // Force inline styles to win over .board-ribbon base rules.
  el.style.display = "flex";
  el.style.flexDirection = "row";
  el.style.position = "static";
  el.style.transform = "none";
  el.style.left = "auto";
  el.style.top = "auto";
  el.style.padding = "2px 6px";
  el.style.borderRight = "0";
  el.style.borderBottom = "0";
  el.style.width = "max-content";
  requestAnimationFrame(() => requestAnimationFrame(() => fitToContent()));
}

// Swap the active ribbon element into the open WinBox.
export function mountRibbonElement(el) {
  if (!el) return;
  mountEl(el);
}

export function openRibbonWindow(el) {
  if (wb) {
    mountRibbonElement(el);
    return;
  }
  const geo = loadGeo();
  const x = geo?.x ?? 8;
  const y = geo?.y ?? HEADER_H;
  wb = new WinBox({
    title: "Controls",
    class: "sturddle-wb sturddle-wb-ribbon no-full no-resize no-min no-max",
    width: 480,
    height: 70,
    x,
    y,
    top: HEADER_H,
    onclose() {
      restoreElementToOrigin();
      currentEl = null;
      originalParent = null;
      originalNextSibling = null;
      wb = null;
      if (!programmaticClose) {
        window.dispatchEvent(new CustomEvent("sturddle:ribbon-float-closed"));
      }
    },
    onmove() { saveGeo(); },
  });
  mountEl(el);
}

function clearInlineStyles(el) {
  for (const p of ["display","flexDirection","position","transform","left","top","padding","borderRight","borderBottom","width"]) {
    el.style[p] = "";
  }
}

function fitToContent() {
  if (!wb || !currentEl) return;
  const HEADER_PX = 20; // thin title bar height from CSS
  const w = currentEl.scrollWidth;
  const h = currentEl.scrollHeight + HEADER_PX;
  wb.resize(w, h);
}

export function closeRibbonWindow() {
  if (!wb) return;
  programmaticClose = true;
  wb.close(); // onclose nulls wb and currentEl
  programmaticClose = false;
}
