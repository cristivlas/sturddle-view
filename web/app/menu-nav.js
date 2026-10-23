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

import { escapeHtml, nextNavIndex } from "./wb-utils.js";
import { ACTIVATE_KEYS } from "./keys.js";

// Marks an open (sub)menu's <li>; callers' CSS shows its nested list.
export const MENU_OPEN_CLASS = "open";

const ROW_KEYS = new Set(["ArrowDown", "ArrowUp", "Home", "End"]);
const MENU_GAP_PX = 4;
const MENU_EDGE_PX = 8;
// A mirrored button's toggled-on state, and the menu's checkmark for it.
const BTN_ACTIVE_CLASS = "is-active";
const ITEM_CHECKED_CLASS = "tmb-active";
const DIR = Object.freeze({ ArrowRight: 1, ArrowLeft: -1 });

const rowButtons = (li) => Array.from(li.querySelectorAll(":scope > button"));
const isEnabled = (b) => !b.disabled;
const navRows = (list) =>
  Array.from(list.querySelectorAll(":scope > li")).filter((li) => rowButtons(li).some(isEnabled));
const submenuOf = (li) => li.querySelector(":scope > ul");

export function focusFirstItem(list) {
  const row = navRows(list)[0];
  if (row) rowButtons(row).find(isEnabled)?.focus();
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

function openSubmenu(li) {
  li.classList.add(MENU_OPEN_CLASS);
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
  keep.forEach((li) => li.classList.add(MENU_OPEN_CLASS));
}

// Wire the menu's own keys. `menu` is the top-level list element. Mouse
// movement over an item focuses it (menu convention), so keyboard focus and
// hover never highlight two different items.
export function attachMenuNav(menu, { close }) {
  menu.addEventListener("pointermove", (ev) => {
    if (ev.pointerType !== "mouse") return;
    const btn = ev.target.closest("button");
    if (!btn || !isEnabled(btn)) return;
    syncSubmenusToHover(menu, btn);
    if (btn !== document.activeElement) btn.focus({ preventScroll: true });
  });
  menu.addEventListener("keydown", (ev) => {
    const handled = handleMenuKey(ev, menu, close);
    if (handled === HANDLED.NO) return;
    if (handled === HANDLED.YES) ev.preventDefault();
    ev.stopPropagation();
  });
}

// Popover menu placement: below the button, else above; fitting neither
// side, it takes the whole viewport height (covering the button) and only
// then scrolls inside. Clamped inside the viewport horizontally.
export function positionMenu(menu, btn) {
  const r = btn.getBoundingClientRect();
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

// Menu rows mirroring real buttons (icon + aria-label or title), by index in
// data-i; a toggled-on button's row is checked. disabled(btn) defaults to
// the button's own state.
export function mirrorItemsHtml(buttons, disabled = (btn) => btn.disabled) {
  return buttons.map((btn, i) => {
    const icon = btn.querySelector("wa-icon")?.getAttribute("name");
    const label = btn.getAttribute("aria-label") || btn.title;
    const cls = btn.classList.contains(BTN_ACTIVE_CLASS) ? ` ${ITEM_CHECKED_CLASS}` : "";
    return `<li><button type="button" class="tmb-dd-item${cls}" data-i="${i}"${disabled(btn) ? " disabled" : ""}>`
      + `${icon ? `<wa-icon name="${escapeHtml(icon)}"></wa-icon>` : ""}${escapeHtml(label)}</button></li>`;
  }).join("");
}
