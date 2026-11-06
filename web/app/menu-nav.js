// Keyboard behavior for dropdown menus (ARIA menu pattern), shared by the
// Arena menubar dropdowns and the data-table columns menu. Rows are a list's
// direct <li> children holding buttons; a row may hold several (columns menu:
// toggle + move arrows). Up/Down/Home/End move between rows, Left/Right
// within one. A row whose <li> nests a list is a submenu: Right/Enter/Space
// open it, Left/Escape close it. Escape at the top level and Tab close the
// whole menu through the caller's close(), which refocuses the opener (Tab
// then continues to the next tab stop).
// Callers focusFirstItem() on open, so keys land here from the start.
// Also the popover menus' shared opener, placement and mirrored-button rows.

import { escapeHtml, nextNavIndex, PCT_SUFFIX } from "./wb-utils.js";
import { ACTIVATE_KEYS } from "./keys.js";

// Marks an open (sub)menu's <li>; callers' CSS shows its nested list.
export const MENU_OPEN_CLASS = "open";
// Checkmark on a menu item (styles.css .tmb-dd-item.tmb-active).
export const MENU_CHECKED_CLASS = "tmb-active";

const ROW_KEYS = new Set(["ArrowDown", "ArrowUp", "Home", "End"]);
const MENU_GAP_PX = 4;
const MENU_EDGE_PX = 8;
// A mirrored button's toggled-on state; its menu row gets MENU_CHECKED_CLASS.
const BTN_ACTIVE_CLASS = "is-active";
// On a menu in the viewport's right half: its flyouts open leftward.
const FLYOUT_LEFT_CLASS = "flyout-left";
const SLIDER_ROW_CLASS = "menu-slider";
const SLIDER_VAL_CLASS = "menu-slider-val";
const SLIDER_KEY_ATTR = "data-slider";
const FIXED_POSITION = "fixed";
// On a menu the mouse is driving: styles.css drops its focus ring (hover
// focuses items); a key press brings it back.
const POINTER_CLASS = "menu-pointer";
const MOUSE_RIGHT_BUTTON = 2;
const TABINDEX_ATTR = "tabindex";
// Marks a makeMenuHost element; styles.css keeps it ringless.
const MENU_HOST_ATTR = "data-menu-host";
const DIR = Object.freeze({ ArrowRight: 1, ArrowLeft: -1 });

const rowButtons = (li) => Array.from(li.querySelectorAll(":scope > button"));
const isEnabled = (b) => !b.disabled;
const navRows = (list) =>
  Array.from(list.querySelectorAll(":scope > li")).filter((li) => rowButtons(li).some(isEnabled));
const submenuOf = (li) => li.querySelector(":scope > ul");

// A buttonless (sub)menu, e.g. a slider flyout, focuses its first control.
const FALLBACK_CONTROL = "input, wa-slider";

export function focusFirstItem(list) {
  const row = navRows(list)[0];
  if (row) rowButtons(row).find(isEnabled)?.focus();
  else list.querySelector(FALLBACK_CONTROL)?.focus();
}

// Enabled button in `row` nearest to position `pos` (keeps the column on
// Up/Down when rows hold several buttons).
function nearestEnabled(row, pos) {
  let best = null;
  rowButtons(row).forEach((b, i) => {
    if (isEnabled(b) && (!best || Math.abs(i - pos) < Math.abs(best.i - pos))) best = { b, i };
  });
  return best?.b ?? null;
}

// Next enabled button from `i` in direction `dir`, or null at the row edge.
function stepInRow(btns, i, dir) {
  for (let j = i + dir; j >= 0 && j < btns.length; j += dir) {
    if (isEnabled(btns[j])) return btns[j];
  }
  return null;
}

// A fixed pane (a scrolling menu's, which would clip an absolute one) goes
// beside its trigger, right if it fits else left, inside the viewport.
function placeFixedSubmenu(li, sub) {
  const r = li.getBoundingClientRect();
  const w = sub.offsetWidth;
  const left = r.right + w <= window.innerWidth - MENU_EDGE_PX ? r.right : Math.max(MENU_EDGE_PX, r.left - w);
  sub.style.left = left + "px";
  sub.style.top = Math.max(MENU_EDGE_PX, Math.min(r.top, window.innerHeight - MENU_EDGE_PX - sub.offsetHeight)) + "px";
}

// Lifts an open submenu's pane (CSS top: 0, level with its trigger) just
// enough to keep its bottom inside the viewport, never past the top edge.
function fitSubmenuToViewport(li) {
  const sub = submenuOf(li);
  if (getComputedStyle(sub).position === FIXED_POSITION) {
    placeFixedSubmenu(li, sub);
    return;
  }
  sub.style.top = "";
  const r = sub.getBoundingClientRect();
  const overflow = r.bottom - (window.innerHeight - MENU_EDGE_PX);
  if (overflow > 0) sub.style.top = -Math.min(overflow, Math.max(0, r.top - MENU_EDGE_PX)) + "px";
}

function showSubmenu(li) {
  if (li.classList.contains(MENU_OPEN_CLASS)) return;
  li.classList.add(MENU_OPEN_CLASS);
  fitSubmenuToViewport(li);
}

function openSubmenu(li) {
  showSubmenu(li);
  focusFirstItem(submenuOf(li));
}

function closeSubmenu(li) {
  li.classList.remove(MENU_OPEN_CLASS);
  rowButtons(li)[0]?.focus();
}

// handleMenuKey outcome: NO = not a menu key (left alone); YES = handled,
// default suppressed; PASS_DEFAULT = handled, browser default still runs.
const HANDLED = Object.freeze({ NO: 0, YES: 1, PASS_DEFAULT: 2 });

function handleMenuKey(ev, menu, close) {
  const btn = ev.target.closest("button");
  const row = btn?.closest("li");
  const list = row?.parentElement;
  if (!btn || !list || !menu.contains(list)) return HANDLED.NO;
  const sub = submenuOf(row);
  const parentLi = list === menu ? null : list.closest("li");
  const btns = rowButtons(row);
  const i = btns.indexOf(btn);
  if (ROW_KEYS.has(ev.key)) {
    const rows = navRows(list);
    const next = rows[nextNavIndex(ev.key, rows.indexOf(row), rows.length, 1)];
    if (next) nearestEnabled(next, i)?.focus();
  } else if (Object.hasOwn(DIR, ev.key)) {
    const dir = DIR[ev.key];
    const target = stepInRow(btns, i, dir);
    if (target) target.focus();
    else if (dir > 0 && sub) openSubmenu(row);
    else if (dir < 0 && parentLi) closeSubmenu(parentLi);
  } else if (ACTIVATE_KEYS.has(ev.key) && sub) {
    openSubmenu(row);
  } else if (ev.key === "Escape") {
    if (parentLi) closeSubmenu(parentLi);
    else close();
  } else if (ev.key === "Tab") {
    // close() refocuses the opener; the browser's Tab then moves on from it
    // to the next (or, with Shift, previous) tab stop.
    close();
    return HANDLED.PASS_DEFAULT;
  } else {
    return HANDLED.NO;
  }
  return HANDLED.YES;
}

// Hovered `btn` pins open the submenus on its path (its own, if it is a
// trigger, and the ones it sits in) and closes the rest, so focus never
// rests inside a submenu that hid when the mouse left it.
function syncSubmenusToHover(menu, btn) {
  const keep = new Set();
  for (let li = btn.closest("li"); li && menu.contains(li); li = li.parentElement.closest("li")) {
    if (submenuOf(li)) keep.add(li);
  }
  for (const li of menu.querySelectorAll(`li.${MENU_OPEN_CLASS}`)) {
    if (!keep.has(li)) li.classList.remove(MENU_OPEN_CLASS);
  }
  keep.forEach(showSubmenu);
}

// Wire the menu's own keys. `menu` is the top-level list element. Mouse
// movement over an item focuses it (menu convention), so keyboard focus and
// hover never highlight two different items.
export function attachMenuNav(menu, { close }) {
  menu.addEventListener("pointermove", (ev) => {
    if (ev.pointerType !== "mouse") return;
    const btn = ev.target.closest("button");
    if (!btn || !isEnabled(btn)) return;
    menu.classList.add(POINTER_CLASS);
    syncSubmenusToHover(menu, btn);
    if (btn !== document.activeElement) btn.focus({ preventScroll: true });
  });
  // Opened with nothing focused (a click, a right-click): the first row key
  // enters the menu instead of moving whatever sits beneath it.
  const onOutsideKey = (ev) => {
    // Removed while open: no closing beforetoggle fired.
    if (!menu.matches(":popover-open")) {
      document.removeEventListener("keydown", onOutsideKey, true);
      return;
    }
    if (!ROW_KEYS.has(ev.key) || menu.contains(document.activeElement)) return;
    ev.preventDefault();
    ev.stopPropagation();
    menu.classList.remove(POINTER_CLASS);
    focusFirstItem(menu);
  };
  menu.addEventListener("beforetoggle", (ev) => {
    if (ev.newState === "open") {
      menu.classList.remove(POINTER_CLASS);
      document.addEventListener("keydown", onOutsideKey, true);
    } else {
      document.removeEventListener("keydown", onOutsideKey, true);
    }
  });
  menu.addEventListener("keydown", (ev) => {
    menu.classList.remove(POINTER_CLASS);
    const handled = handleMenuKey(ev, menu, close);
    if (handled === HANDLED.NO) return;
    if (handled === HANDLED.YES) ev.preventDefault();
    ev.stopPropagation();
  });
  // A scrolled menu moves its triggers; fixed flyouts follow.
  menu.addEventListener("scroll", () => {
    for (const li of menu.querySelectorAll(`li.${MENU_OPEN_CLASS}`)) fitSubmenuToViewport(li);
  });
}

export function positionMenu(menu, btn) {
  positionMenuAt(menu, btn.getBoundingClientRect());
}

// Popover menu placement: below the anchor { top, bottom, left }, else above;
// fitting neither side, it takes the whole viewport height (covering the
// anchor) and only then scrolls inside. Clamped inside the viewport horizontally.
export function positionMenuAt(menu, r) {
  menu.style.maxHeight = "";
  const h = menu.offsetHeight;
  const roomBelow = window.innerHeight - r.bottom - MENU_GAP_PX - MENU_EDGE_PX;
  const roomAbove = r.top - MENU_GAP_PX - MENU_EDGE_PX;
  let room, top;
  if (h <= roomBelow) {
    room = roomBelow;
    top = r.bottom + MENU_GAP_PX;
  } else if (h <= roomAbove) {
    room = roomAbove;
    top = r.top - MENU_GAP_PX - h;
  } else {
    room = Math.max(0, window.innerHeight - 2 * MENU_EDGE_PX);
    top = window.innerHeight - MENU_EDGE_PX - Math.min(h, room);
  }
  menu.style.maxHeight = room + "px";
  menu.style.top = top + "px";
  const maxLeft = window.innerWidth - menu.offsetWidth - MENU_EDGE_PX;
  menu.style.left = Math.max(MENU_EDGE_PX, Math.min(r.left, maxLeft)) + "px";
}

// Opener for a top-layer popover menu. Light dismiss closes the menu on
// pointerup, before the click: pointerdown records whether it was open so a
// second click closes it, not reopens. open(ev) renders, shows and places it.
export function wireMenuToggle(btn, menu, open) {
  const isOpen = () => menu.matches(":popover-open");
  let wasOpen = false;
  btn.addEventListener("pointerdown", () => { wasOpen = isOpen(); });
  btn.addEventListener("click", (ev) => {
    const close = wasOpen || isOpen();
    wasOpen = false;
    if (!close) open(ev);
    else if (isOpen()) menu.hidePopover();
  });
}

// Right-clicks that a menu (a table's Columns menu) already took, so an outer
// panel menu leaves them be. defaultPrevented can't tell: main.js cancels
// every contextmenu (no browser menu).
const claimedContextMenus = new WeakSet();
export const claimContextMenu = (ev) => { claimedContextMenus.add(ev); };
export const isContextMenuClaimed = (ev) => claimedContextMenus.has(ev);
// The Menu key (or Shift+F10), not a right-click: its menu opens focused.
export const isKeyboardContextMenu = (ev) => ev.button !== MOUSE_RIGHT_BUTTON;

// The Menu key fires at the focused element: make el take focus on a click
// (not Tab), so its contextmenu listener hears it. Never on a shadow host (a
// wa-tab-group): a negative tabindex drops its slotted subtree from Tab order.
// Returns the undo.
export function makeMenuHost(el) {
  if (el.hasAttribute(TABINDEX_ATTR) || el.shadowRoot) return () => {};
  el.tabIndex = -1;
  el.setAttribute(MENU_HOST_ATTR, "");
  return () => { el.removeAttribute(TABINDEX_ATTR); el.removeAttribute(MENU_HOST_ATTR); };
}

// Flyouts open toward the side with more room; call once the menu is placed.
export function setFlyoutSide(menu) {
  const r = menu.getBoundingClientRect();
  menu.classList.toggle(FLYOUT_LEFT_CLASS, r.left + r.width / 2 > window.innerWidth / 2);
}

// A submenu row: trigger button plus its flyout list.
export function submenuHtml(label, inner, disabled = false) {
  return `<li class="tmb-dd-submenu"><button type="button" class="tmb-dd-item"${disabled ? " disabled" : ""}>${label}</button>`
    + `<ul class="tmb-dropdown">${inner}</ul></li>`;
}

// A slider flyout row with its live readout (value + unit). key tells a
// menu's several slider rows apart for wireSliderRow.
export function sliderRowHtml({ min = 0, max, step, value, unit = PCT_SUFFIX, key = null }) {
  const keyAttr = key == null ? "" : ` ${SLIDER_KEY_ATTR}="${key}"`;
  return `<li class="${SLIDER_ROW_CLASS}"${keyAttr}>`
    + `<wa-slider size="small" min="${min}" max="${max}" step="${step}" value="${value}"></wa-slider>`
    + `<span class="${SLIDER_VAL_CLASS}">${value}${unit}</span></li>`;
}

// Wires a slider row in `menu` (the one with `key`, else the first), if
// there is one: onInput(value) while dragging, onChange(value) on release.
export function wireSliderRow(menu, { onInput, onChange, unit = PCT_SUFFIX, key = null }) {
  const keySel = key == null ? "" : `[${SLIDER_KEY_ATTR}="${key}"]`;
  const row = menu.querySelector(`.${SLIDER_ROW_CLASS}${keySel}`);
  const slider = row?.querySelector("wa-slider");
  if (!slider) return;
  const val = row.querySelector(`.${SLIDER_VAL_CLASS}`);
  slider.addEventListener("input", () => {
    onInput(Number(slider.value));
    val.textContent = `${slider.value}${unit}`;
  });
  slider.addEventListener("change", () => onChange(Number(slider.value)));
}

// Menu rows mirroring real buttons (icon + aria-label or title), by index in
// data-i; a toggled-on button's row is checked. disabled(btn) defaults to
// the button's own state.
export function mirrorItemsHtml(buttons, disabled = (btn) => btn.disabled) {
  return buttons.map((btn, i) => {
    const icon = btn.querySelector("wa-icon")?.getAttribute("name");
    const label = btn.getAttribute("aria-label") || btn.title;
    const cls = btn.classList.contains(BTN_ACTIVE_CLASS) ? ` ${MENU_CHECKED_CLASS}` : "";
    return `<li><button type="button" class="tmb-dd-item${cls}" data-i="${i}"${disabled(btn) ? " disabled" : ""}>`
      + `${icon ? `<wa-icon name="${escapeHtml(icon)}"></wa-icon>` : ""}${escapeHtml(label)}</button></li>`;
  }).join("");
}
