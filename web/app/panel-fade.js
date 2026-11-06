// Panel transparency: fades a panel's solid backgrounds -- the panel element
// and its ancestors up to the view root (the window, dock slot or tab group
// holding it) -- plus its tables' header cells and lines, so what lies behind
// (the wallpaper) shows through. Each panel owns one fader; a table only adds
// its share to its panel's. Plus the Transparency submenu that sets it, and a
// right-click menu for panels without a menu of their own (which can carry the
// panel's own actions too).

import { APP_EVT } from "./app-events.js";
import { mqMobile, mqMobileHPlay } from "./breakpoints.js";
import {
  attachMenuNav, claimContextMenu, focusFirstItem, isContextMenuClaimed, isKeyboardContextMenu,
  makeMenuHost, MENU_CHECKED_CLASS, positionMenuAt, sliderRowHtml, submenuHtml, wireSliderRow,
} from "./menu-nav.js";
import { loadJson, saveJson } from "./storage.js";
import { escapeHtml, PCT_SUFFIX } from "./wb-utils.js";

// Panel-menu action labels shared across panels.
export const COPY_LABEL = "Copy";
export const RESET_LABEL = "Reset";

const FADE_LABEL = "Transparency";
const FADE_MAX_PCT = 100;
const FADE_STEP_PCT = 5;
// A faded element and its faded background; a table with faded cell lines and
// their colors per side (styles.css).
const FADE_ATTR = "data-fade";
const FADE_BG_VAR = "--fade-bg";
const FADE_LINES_ATTR = "data-fade-lines";
const PANEL_MENU_CLASS = "tmb-dropdown panel-fade-menu";
// A panel-menu action's index in its actions.
const ACTION_ATTR = "data-action-i";
const SEPARATOR_HTML = `<li class="tmb-separator"></li>`;
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

// Transparency's slider: its label and range.
const FADE_SLIDER = Object.freeze({ label: FADE_LABEL, max: FADE_MAX_PCT, step: FADE_STEP_PCT });

// A slider submenu: { label, min?, max, step, unit? } at `value`; key tells
// a menu's sliders apart.
function sliderSubmenuHtml({ label, min, max, step, unit }, value, key = null) {
  return submenuHtml(label, sliderRowHtml({ min, max, step, unit, value, key }));
}

// The Transparency submenu at `pct`; "" where fading is off (mobile).
export function fadeMenuHtml(pct) {
  return isMobile() ? "" : sliderSubmenuHtml(FADE_SLIDER, pct);
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

// A panel's fader: fades the panel, its ancestors up to the view root (the
// window, dock slot or tab group holding it) and its parts' elements, at the
// pct saved under storageKey. A part ({ ownEls(), lineTables }) is a table's
// share: its extra elements and the tables whose lines fade. It re-reads the
// backgrounds on rescan() (a part rebuilt its elements), when the panel moved
// (dock <-> window), a table's first row came or a wallpaper came or went
// (fills with it), else repaints on a new pct only. Off on mobile and while
// hidden. A panel built after its fader (a window's body, which builds its
// parts) is bound with attach(). Faders never share a filled ancestor (one per
// window or tab group, none nested), so marks need no owner.
export function createPanelFade(panel, { storageKey }) {
  let pct = loadJson(storageKey, 0);
  const parts = new Set();
  let hosts = [];
  let marks = []; // { el, attr, colors: [[var, el's own color]] }
  let linesPending = []; // tables scanned before their first row came
  let painted = null;
  const lineTables = () => [...parts].flatMap((p) => p.lineTables ?? []);
  const hostChain = () => {
    const stop = panel.closest(VIEW_ROOT_SEL);
    const els = [];
    for (let el = panel; el && el !== stop && el !== document.body; el = el.parentElement) els.push(el);
    return els;
  };
  function release() {
    for (const { el, attr, colors } of marks) {
      el.removeAttribute(attr);
      for (const [v] of colors) el.style.removeProperty(v);
    }
    marks = [];
    hosts = [];
    painted = null;
  }
  function scan(chain) {
    const own = [...parts].flatMap((p) => p.ownEls?.() ?? []);
    for (const el of new Set([...own, ...chain])) {
      const bg = idleBg(el);
      if (bg !== NO_BG) marks.push({ el, attr: FADE_ATTR, colors: [[FADE_BG_VAR, bg]] });
    }
    linesPending = [];
    for (const table of lineTables()) {
      const row = lineRow(table);
      if (row) marks.push({ el: table, attr: FADE_LINES_ATTR, colors: lineColors(row) });
      else linesPending.push(table);
    }
  }
  function apply(force = false) {
    if (!panel || !pct || isMobile() || !panel.getClientRects().length) {
      release();
      return;
    }
    const chain = hostChain();
    if (force || !marks.length || !sameEls(chain, hosts) || linesPending.some(lineRow)) {
      release();
      hosts = chain;
      scan(chain);
    } else if (pct === painted) {
      return;
    }
    painted = pct;
    for (const { el, attr, colors } of marks) {
      for (const [v, c] of colors) el.style.setProperty(v, `color-mix(in srgb, ${c}, transparent ${pct}${PCT_SUFFIX})`);
      el.setAttribute(attr, "");
    }
  }
  // A move between hosts resizes the panel, and a table's body resizes as its
  // first rows come.
  const refresh = () => apply();
  const observer = new ResizeObserver(refresh);
  const watchBodies = (tables, on) => {
    for (const body of tables.flatMap((t) => [...t.tBodies])) {
      if (on) observer.observe(body);
      else observer.unobserve(body);
    }
  };
  MOBILE_QUERIES.forEach((mq) => mq.addEventListener("change", refresh));
  const rescan = () => apply(true);
  window.addEventListener(APP_EVT.WALLPAPER_TOGGLED, rescan);
  function attach(el) {
    panel = el;
    observer.observe(panel);
    apply(true);
  }
  if (panel) attach(panel);
  return {
    get pct() { return pct; },
    attach,
    // Live (a slider drag); save() keeps it.
    set(next) {
      pct = next;
      apply();
    },
    save() { saveJson(storageKey, pct); },
    rescan,
    // Returns the part's removal.
    addPart(part) {
      parts.add(part);
      watchBodies(part.lineTables ?? [], true);
      apply(true);
      return () => {
        parts.delete(part);
        watchBodies(part.lineTables ?? [], false);
        apply(true);
      };
    },
    destroy() {
      observer.disconnect();
      MOBILE_QUERIES.forEach((mq) => mq.removeEventListener("change", refresh));
      window.removeEventListener(APP_EVT.WALLPAPER_TOGGLED, rescan);
      release();
    },
  };
}

// A panel's right-click menu (a top-layer popover, so no host clips it):
// actions [{ label, run, checked?, disabled? }] on top (checked/disabled are
// read at each open), then, with a fade (its fader), its Transparency, then
// sliders [{ label, min?, max, step, unit?, get, set, save }] (set while
// dragging, save on release), then tailActions; separators between. Opens
// only while shown(ev) holds. A right-click a menu inside already took (a
// table's Columns menu) is left to it. hosts take focus on a click so the
// Menu key reaches the menu: the panel, or its parts when it is a shadow host
// (makeMenuHost). Returns the teardown.
export function attachPanelMenu(panel, {
  fade = null, actions = [], sliders = [], tailActions = [], shown = () => true, hosts = [panel],
} = {}) {
  const allSliders = fade ? [{
    ...FADE_SLIDER, get: () => fade.pct, set: (pct) => fade.set(pct), save: () => fade.save(),
  }, ...sliders] : sliders;
  const menu = document.createElement("ul");
  menu.className = PANEL_MENU_CLASS;
  menu.setAttribute("popover", "auto");
  const close = () => { if (menu.matches(":popover-open")) menu.hidePopover(); };
  attachMenuNav(menu, { close });
  // Where focus returns when the menu closes with it inside (Esc).
  let returnFocus = null;
  menu.addEventListener("beforetoggle", (ev) => {
    if (ev.newState === "closed" && menu.contains(document.activeElement)) returnFocus?.focus();
  });
  menu.addEventListener("click", (ev) => {
    const btn = ev.target.closest(`button[${ACTION_ATTR}]`);
    if (!btn) return;
    close();
    [...actions, ...tailActions][btn.getAttribute(ACTION_ATTR)].run();
  });
  document.body.append(menu);

  const actionHtml = (a, i) => {
    const cls = a.checked?.() ? ` ${MENU_CHECKED_CLASS}` : "";
    const dis = a.disabled?.() ? " disabled" : "";
    return `<li><button type="button" class="tmb-dd-item${cls}" ${ACTION_ATTR}="${i}"${dis}>`
      + `${escapeHtml(a.label)}</button></li>`;
  };

  function syncActionStates() {
    const all = [...actions, ...tailActions];
    for (const btn of menu.querySelectorAll(`button[${ACTION_ATTR}]`)) {
      const a = all[btn.getAttribute(ACTION_ATTR)];
      btn.disabled = !!a.disabled?.();
      btn.classList.toggle(MENU_CHECKED_CLASS, !!a.checked?.());
    }
  }

  // Desktop only: on a phone a long-press would pop it.
  function onContextMenu(ev) {
    if (isContextMenuClaimed(ev) || isMobile() || !shown(ev)) return;
    const html = [
      actions.map(actionHtml).join(""),
      allSliders.map((s, i) => sliderSubmenuHtml(s, s.get(), i)).join(""),
      tailActions.map((a, i) => actionHtml(a, actions.length + i)).join(""),
    ].filter(Boolean).join(SEPARATOR_HTML);
    if (!html) return;
    ev.preventDefault();
    // Taken: an outer menu (the wallpaper's, on a bare region) leaves it be.
    claimContextMenu(ev);
    menu.innerHTML = html;
    // A drag can flip an action's state (a Reset at defaults): re-read it live.
    const onInput = (s) => (v) => { s.set(v); syncActionStates(); };
    allSliders.forEach((s, i) => wireSliderRow(menu, {
      key: i, unit: s.unit, onInput: onInput(s), onChange: s.save,
    }));
    if (!menu.matches(":popover-open")) {
      returnFocus = document.activeElement;
      menu.showPopover();
    }
    positionMenuAt(menu, { top: ev.clientY, bottom: ev.clientY, left: ev.clientX });
    if (isKeyboardContextMenu(ev)) focusFirstItem(menu);
  }
  panel.addEventListener("contextmenu", onContextMenu);
  const unhosts = hosts.map(makeMenuHost);

  return () => {
    panel.removeEventListener("contextmenu", onContextMenu);
    unhosts.forEach((undo) => undo());
    menu.remove();
  };
}
