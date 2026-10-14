// Keyboard behavior for dropdown menus (ARIA menu pattern), shared by the
// Arena menubar dropdowns and the data-table columns menu. Rows are a list's
// direct <li> children holding buttons; a row may hold several (columns menu:
// toggle + move arrows). Up/Down/Home/End move between rows, Left/Right
// within one. A row whose <li> nests a list is a submenu: Right/Enter/Space
// open it, Left/Escape close it. Escape at the top level and Tab close the
// whole menu through the caller's close(), which refocuses the opener (Tab
// then continues to the next tab stop).
// Callers focusFirstItem() on open, so keys land here from the start.

import { nextNavIndex } from "./wb-utils.js";

// Marks an open (sub)menu's <li>; callers' CSS shows its nested list.
export const MENU_OPEN_CLASS = "open";

const ROW_KEYS = new Set(["ArrowDown", "ArrowUp", "Home", "End"]);
const ACTIVATE_KEYS = new Set(["Enter", " "]);
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

// Wire the menu's own keys. `menu` is the top-level list element.
export function attachMenuNav(menu, { close }) {
  menu.addEventListener("keydown", (ev) => {
    const handled = handleMenuKey(ev, menu, close);
    if (handled === HANDLED.NO) return;
    if (handled === HANDLED.YES) ev.preventDefault();
    ev.stopPropagation();
  });
}
