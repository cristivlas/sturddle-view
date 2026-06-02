// Floating ribbon: wraps the active ribbon element (.board-ribbon or
// .tournaments-ribbon) in a WinBox. Orientation toggles between horizontal
// and vertical via a title-bar control; orientation and position persist
// to localStorage. On user-close, fires sturddle:ribbon-float-closed so
// main.js can revert to edge-docked.

import { APP_EVT } from "./app-events.js";
import { STORAGE_KEY } from "./storage-keys.js";

// LocalStorage keys: side selection ("left"|"right"|"float"), the
// WinBox geometry, and orientation ("h"|"v") for the floating mode.
export const RIBBON_SIDE_KEY = STORAGE_KEY.RIBBON_SIDE;
const GEO_KEY = STORAGE_KEY.RIBBON_GEO;
const ORIENT_KEY = STORAGE_KEY.RIBBON_ORIENT;
const HEADER_H = 44;     // px -- nav header height (top boundary)
const ORIENT_H = "h";
const ORIENT_V = "v";
const DEFAULT_GEO_X = 8;      // px -- default float window left offset
const WB_VERT_W = 50;         // px -- WinBox width when ribbon is vertical
const WB_VERT_H = 480;        // px -- WinBox height when ribbon is vertical
const WB_HORIZ_W = 480;       // px -- WinBox width when ribbon is horizontal
const WB_HORIZ_H = 70;        // px -- WinBox height when ribbon is horizontal
const WB_MIN_W = 80;          // px -- WinBox minimum width
const WB_MIN_H = 40;          // px -- WinBox minimum height
const WB_TITLE_H = 20;        // px -- WinBox title bar height (from CSS)

let wb = null;
let wbOuter = null; // outer .winbox element; cached so we don't querySelector
let currentEl = null;
let originalParent = null; // where currentEl lived before we moved it
let originalNextSibling = null; // sibling to insertBefore on restore
let programmaticClose = false;

function loadGeo() {
  try { return JSON.parse(localStorage.getItem(GEO_KEY)) || null; } catch { return null; }
}

function clampGeo(x, y, w, h) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  return {
    x: Math.max(0, Math.min(x, vw - w)),
    y: Math.max(HEADER_H, Math.min(y, vh - h)),
  };
}

function saveGeo() {
  if (!wb) return;
  const clamped = clampGeo(wb.x, wb.y, wb.width, wb.height);
  localStorage.setItem(GEO_KEY, JSON.stringify(clamped));
}

function loadOrient() {
  return localStorage.getItem(ORIENT_KEY) === ORIENT_V ? ORIENT_V : ORIENT_H;
}

function saveOrient(o) {
  localStorage.setItem(ORIENT_KEY, o);
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

function applyOrientation(el) {
  const vertical = loadOrient() === ORIENT_V;
  // Force inline styles to win over base rules of either ribbon class.
  el.style.display = "flex";
  el.style.flexDirection = vertical ? "column" : "row";
  el.style.position = "static";
  el.style.transform = "none";
  el.style.left = "auto";
  el.style.top = "auto";
  // Swap padding so the cross-axis is the tight one in both orientations.
  el.style.padding = vertical ? "6px 2px" : "2px 6px";
  // Zero the edge borders from both ribbon classes' base rules.
  el.style.border = "0";
  el.style.width = "max-content";
  el.style.height = "max-content";
  // Title text is hidden in vertical mode (no room next to the rotate ctrl).
  if (wbOuter) {
    wbOuter.classList.toggle("ribbon-vertical", vertical);
    wbOuter.classList.toggle("ribbon-horizontal", !vertical);
  }
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
  applyOrientation(el);
  requestAnimationFrame(() => requestAnimationFrame(() => fitToContent()));
}

function toggleOrientation() {
  if (!currentEl || !wb) return;
  const next = loadOrient() === ORIENT_V ? ORIENT_H : ORIENT_V;
  saveOrient(next);
  applyOrientation(currentEl);
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
  const isVertical = loadOrient() === ORIENT_V;
  const rawW = isVertical ? WB_VERT_W : WB_HORIZ_W;
  const rawH = isVertical ? WB_VERT_H : WB_HORIZ_H;
  const { x, y } = clampGeo(geo?.x ?? DEFAULT_GEO_X, geo?.y ?? HEADER_H, rawW, rawH);
  wb = new WinBox({
    title: "Controls",
    class: "sturddle-wb sturddle-wb-ribbon no-full no-resize no-min no-max",
    width: rawW,
    height: rawH,
    minwidth: WB_MIN_W,
    minheight: WB_MIN_H,
    x,
    y,
    top: HEADER_H,
    onclose() {
      restoreElementToOrigin();
      currentEl = null;
      originalParent = null;
      originalNextSibling = null;
      wb = null;
      wbOuter = null;
      if (!programmaticClose) {
        window.dispatchEvent(new CustomEvent(APP_EVT.RIBBON_FLOAT_CLOSED));
      }
    },
    onmove() { saveGeo(); },
  });
  // Orientation toggle button on the title bar.
  wb.addControl({
    class: "wb-ribbon-rotate-ctrl",
    index: 0,
    click: toggleOrientation,
  });
  // Cache the outer WinBox element and set a11y label on the new control.
  wbOuter = wb.body?.parentElement || null;
  const rotateBtn = wbOuter?.querySelector(".wb-ribbon-rotate-ctrl");
  if (rotateBtn) {
    rotateBtn.title = "Rotate orientation";
    rotateBtn.setAttribute("aria-label", "Rotate orientation");
  }
  mountEl(el);
}

function clearInlineStyles(el) {
  for (const p of ["display","flexDirection","position","transform","left","top","padding","border","width","height"]) {
    el.style[p] = "";
  }
}

function fitToContent() {
  if (!wb || !currentEl) return;
  const HEADER_PX = WB_TITLE_H;
  const w = currentEl.scrollWidth;
  const h = currentEl.scrollHeight + HEADER_PX;
  wb.resize(w, h);
}

export function nudgeRibbonToViewport() {
  if (!wb) return;
  const { x, y } = clampGeo(wb.x, wb.y, wb.width, wb.height);
  if (x !== wb.x || y !== wb.y) wb.move(x, y);
}

export function closeRibbonWindow() {
  if (!wb) return;
  programmaticClose = true;
  wb.close(); // onclose nulls wb and currentEl
  programmaticClose = false;
}
