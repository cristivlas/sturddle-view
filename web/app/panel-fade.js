// Panel transparency: fades a panel's solid backgrounds -- the panel element,
// its ancestors up to the view root (the window or dock slot holding it) and
// any extra elements of its own -- plus, optionally, tables' cell lines, so
// what lies behind (the wallpaper) shows through. Each panel has one fader;
// a table hands its extras (header cells, lines) to it. Plus the Transparency
// submenu that sets it.

import { mqMobile, mqMobileHPlay } from "./breakpoints.js";
import { sliderRowHtml, submenuHtml } from "./menu-nav.js";
import { PCT_SUFFIX } from "./wb-utils.js";

const FADE_LABEL = "Transparency";
const FADE_MAX_PCT = 100;
const FADE_STEP_PCT = 5;
// A faded element (valued with its fader's id) and its faded background; a
// table with faded cell lines and their colors per side (styles.css).
const FADE_ATTR = "data-fade";
const FADE_BG_VAR = "--fade-bg";
const FADE_LINES_ATTR = "data-fade-lines";
const FADE_LINE_VAR = Object.freeze({
  top: "--fade-line-t", right: "--fade-line-r", bottom: "--fade-line-b", left: "--fade-line-l",
});
// getComputedStyle's backgroundColor for no fill, border style for no line.
const NO_BG = "rgba(0, 0, 0, 0)";
const NO_LINE = "none";
// Outermost host a fade reaches (the view; a wallpaper region inside it
// paints over its own ancestors, so they needn't stop the walk).
const VIEW_ROOT_SEL = "#perspective-root";
// Phone layouts (Play counts landscape too): no wallpaper, so no Transparency.
const MOBILE_QUERIES = [mqMobile, mqMobileHPlay];
const isMobile = () => MOBILE_QUERIES.some((mq) => mq.matches);

// The Transparency submenu at `pct`; "" where fading is off (mobile).
export function fadeMenuHtml(pct) {
  if (isMobile()) return "";
  return submenuHtml(FADE_LABEL, sliderRowHtml({ max: FADE_MAX_PCT, step: FADE_STEP_PCT, value: pct }));
}

const sameEls = (a, b) => a.length === b.length && a.every((el, i) => el === b[i]);

// An element's own background; a hovered header cell's is read off an
// unhovered copy.
function idleBg(el) {
  if (el.tagName !== "TH" || !el.matches(":hover")) return getComputedStyle(el).backgroundColor;
  const copy = el.cloneNode(false);
  el.after(copy);
  const bg = getComputedStyle(copy).backgroundColor;
  copy.remove();
  return bg;
}

// The table's first row without a full-width cell (a note row draws no
// column lines).
const lineRow = (table) => Array.from(table.rows).find((r) => !r.querySelector("[colspan]"));

// Each side's line color in `row`, off the first cell that draws that side.
function lineColors(row) {
  const styles = Array.from(row.cells, (c) => getComputedStyle(c));
  return Object.entries(FADE_LINE_VAR).map(([side, v]) => {
    const cs = styles.find((s) => s.getPropertyValue(`border-${side}-style`) !== NO_LINE) ?? styles[0];
    return [v, cs.getPropertyValue(`border-${side}-color`)];
  });
}

let faderSeq = 0;
// A faded element's own background, for a fader scanning it while another
// fader's mark still holds it.
const fadeBase = new WeakMap();

// Fader for `panel` at `pct`; ownEls() lists its extra elements, lineTables
// the tables whose lines fade. apply(pct, rescan) sets the pct; it re-reads
// the backgrounds on rescan (the caller rebuilt its elements), when the panel
// moved (dock <-> window) or a table's first row came, else repaints on a new
// pct only. Hidden or on mobile, it lets go. Marks carry this fader's id, so
// another panel's fader on a shared host (a Studio tab group) never clears
// this one's.
export function createPanelFade(panel, { pct = 0, ownEls = () => [], lineTables = [] } = {}) {
  const id = String(++faderSeq);
  let hosts = [];
  let marks = []; // { el, attr, colors: [[var, el's own color]] }
  let linesPending = false;
  let painted = null;
  const hostChain = () => {
    const stop = panel.closest(VIEW_ROOT_SEL);
    const els = [];
    for (let el = panel; el && el !== stop && el !== document.body; el = el.parentElement) els.push(el);
    return els;
  };
  function release() {
    for (const { el, attr, colors } of marks) {
      if (el.getAttribute(attr) !== id) continue;
      el.removeAttribute(attr);
      for (const [v] of colors) el.style.removeProperty(v);
      fadeBase.delete(el);
    }
    marks = [];
    hosts = [];
    painted = null;
  }
  function scan(chain) {
    for (const el of [...ownEls(), ...chain]) {
      const bg = (el.hasAttribute(FADE_ATTR) && fadeBase.get(el)) || idleBg(el);
      if (bg === NO_BG) continue;
      fadeBase.set(el, bg);
      marks.push({ el, attr: FADE_ATTR, colors: [[FADE_BG_VAR, bg]] });
    }
    linesPending = false;
    for (const table of lineTables) {
      const row = lineRow(table);
      if (row) marks.push({ el: table, attr: FADE_LINES_ATTR, colors: lineColors(row) });
      else linesPending = true;
    }
  }
  function apply(next, rescan = false) {
    pct = next;
    if (!pct || isMobile() || !panel.getClientRects().length) {
      release();
      return;
    }
    const chain = hostChain();
    if (rescan || !marks.length || !sameEls(chain, hosts) || (linesPending && lineTables.some(lineRow))) {
      release();
      hosts = chain;
      scan(chain);
    } else if (pct === painted) {
      return;
    }
    painted = pct;
    for (const { el, attr, colors } of marks) {
      for (const [v, c] of colors) el.style.setProperty(v, `color-mix(in srgb, ${c}, transparent ${pct}${PCT_SUFFIX})`);
      el.setAttribute(attr, id);
    }
  }
  // A move between hosts resizes the panel, and a table's body resizes as its
  // first rows come.
  const refresh = () => apply(pct);
  const observer = new ResizeObserver(refresh);
  for (const el of [panel, ...lineTables.flatMap((t) => [...t.tBodies])]) observer.observe(el);
  MOBILE_QUERIES.forEach((mq) => mq.addEventListener("change", refresh));
  return {
    apply,
    destroy() {
      observer.disconnect();
      MOBILE_QUERIES.forEach((mq) => mq.removeEventListener("change", refresh));
      release();
    },
  };
}
