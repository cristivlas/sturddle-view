// Vertical ribbon overflow: when a vertical ribbon is too short for its
// buttons, the ones that don't fit move -- bottom first -- into a
// vertical-ellipsis button in its last slot, whose menu clicks the real ones.
// Horizontal ribbons (the mobile flip) are left alone.

import { attachMenuNav, focusFirstItem } from "./menu-nav.js";
import { escapeHtml } from "./wb-utils.js";

// On the ribbon while vertical: its items keep their size (styles.css), so
// a short rail overflows instead of squashing the buttons.
const MANAGED_CLASS = "ribbon-overflow-managed";
// On each item moved into the menu (styles.css hides it).
const OVERFLOWED_CLASS = "ribbon-overflowed";
const BTN_CLASS = "ribbon-btn";
const SEP_CLASS = "ribbon-sep";
const MORE_CLASS = "ribbon-more";
const MENU_CLASS = "ribbon-more-menu";
// A ribbon button's toggled-on state, and the menu's checkmark for it.
const BTN_ACTIVE_CLASS = "is-active";
const ITEM_CHECKED_CLASS = "tmb-active";
const MORE_LABEL = "More actions";
const MORE_ICON = "ellipsis-vertical";
const COLUMN = "column";
// Sub-pixel slack when comparing the items' extent with the ribbon's box.
const FIT_SLACK_PX = 0.5;
const MENU_GAP_PX = 4;
const MENU_EDGE_PX = 8;

const isShown = (el) => getComputedStyle(el).display !== "none";

// Wire overflow onto one ribbon element. Returns { detach } for unmount.
export function attachRibbonOverflow(ribbon) {
  const more = document.createElement("button");
  more.type = "button";
  more.className = `${BTN_CLASS} ${MORE_CLASS}`;
  more.setAttribute("aria-label", MORE_LABEL);
  more.title = MORE_LABEL;
  more.setAttribute("aria-haspopup", "menu");
  more.innerHTML = `<wa-icon name="${MORE_ICON}"></wa-icon>`;
  more.hidden = true;
  // Top-layer popover: escapes the ribbon's (and a floating WinBox's) clip.
  const menu = document.createElement("ul");
  menu.className = `tmb-dropdown ${MENU_CLASS}`;
  menu.setAttribute("popover", "auto");
  ribbon.append(more, menu);
  // The buttons the open menu lists, by item index (data-i).
  let targets = [];

  // Laid-out items in ribbon order (the menu and the ellipsis aside).
  const shownItems = () => [...ribbon.children].filter((el) => el !== menu && isShown(el));

  // Do the shown items fit between the ribbon's padded top and bottom?
  // Centered content overflows at both ends, so check both.
  function fits() {
    const items = shownItems();
    if (!items.length) return true;
    const r = ribbon.getBoundingClientRect();
    const cs = getComputedStyle(ribbon);
    return items[0].getBoundingClientRect().top >= r.top + parseFloat(cs.paddingTop) - FIT_SLACK_PX
      && items.at(-1).getBoundingClientRect().bottom <= r.bottom - parseFloat(cs.paddingBottom) + FIT_SLACK_PX;
  }

  // A separator with no shown button before it, or none after it before the
  // next separator / the ellipsis, separates nothing: move it out too.
  function hideStraySeparators() {
    let seenBtn = false;
    let pending = null;
    for (const el of shownItems()) {
      if (el.classList.contains(SEP_CLASS)) {
        if (!seenBtn || pending) el.classList.add(OVERFLOWED_CLASS);
        else pending = el;
      } else if (el === more) {
        pending?.classList.add(OVERFLOWED_CLASS);
        pending = null;
      } else {
        seenBtn = true;
        pending = null;
      }
    }
  }

  function update() {
    const vertical = ribbon.getClientRects().length > 0 && getComputedStyle(ribbon).flexDirection === COLUMN;
    ribbon.classList.toggle(MANAGED_CLASS, vertical);
    for (const el of ribbon.querySelectorAll(`:scope > .${OVERFLOWED_CLASS}`)) el.classList.remove(OVERFLOWED_CLASS);
    more.hidden = true;
    if (!vertical || fits()) {
      if (menu.matches(":popover-open")) menu.hidePopover();
      return;
    }
    more.hidden = false;
    // Popover-anchoring wrappers stay put: only plain buttons can move.
    const movable = shownItems().filter((el) => el.classList.contains(BTN_CLASS) && el !== more);
    while (movable.length && !fits()) {
      movable.pop().classList.add(OVERFLOWED_CLASS);
      hideStraySeparators();
    }
  }

  // Rebuilt on every open, so labels / icons / disabled and on states are current.
  function renderMenu() {
    const hidden = [...ribbon.querySelectorAll(`:scope > .${BTN_CLASS}.${OVERFLOWED_CLASS}`)];
    menu.innerHTML = hidden.map((btn, i) => {
      const icon = btn.querySelector("wa-icon")?.getAttribute("name");
      const label = btn.getAttribute("aria-label") || btn.title;
      const cls = btn.classList.contains(BTN_ACTIVE_CLASS) ? ` ${ITEM_CHECKED_CLASS}` : "";
      return `<li><button type="button" class="tmb-dd-item${cls}" data-i="${i}"${btn.disabled ? " disabled" : ""}>`
        + `${icon ? `<wa-icon name="${escapeHtml(icon)}"></wa-icon>` : ""}${escapeHtml(label)}</button></li>`;
    }).join("");
    targets = hidden;
  }

  // Beside the ellipsis, on whichever side has room; kept inside the viewport.
  function placeMenu() {
    menu.style.maxHeight = `${window.innerHeight - 2 * MENU_EDGE_PX}px`;
    const b = more.getBoundingClientRect();
    const w = menu.offsetWidth;
    const h = menu.offsetHeight;
    const fitsRight = b.right + MENU_GAP_PX + w <= window.innerWidth - MENU_EDGE_PX;
    const left = fitsRight ? b.right + MENU_GAP_PX : b.left - MENU_GAP_PX - w;
    const top = Math.min(b.top, window.innerHeight - MENU_EDGE_PX - h);
    menu.style.left = `${Math.max(MENU_EDGE_PX, left)}px`;
    menu.style.top = `${Math.max(MENU_EDGE_PX, top)}px`;
  }

  const closeMenu = () => { if (menu.matches(":popover-open")) menu.hidePopover(); };

  // Light dismiss closes the menu on the ellipsis' own press, before its
  // click: remember it was open so the click closes rather than reopens.
  let wasOpen = false;
  more.addEventListener("pointerdown", () => { wasOpen = menu.matches(":popover-open"); });
  more.addEventListener("click", () => {
    const close = wasOpen || menu.matches(":popover-open");
    wasOpen = false;
    if (close) {
      closeMenu();
      return;
    }
    renderMenu();
    menu.showPopover();
    placeMenu();
    focusFirstItem(menu);
  });
  menu.addEventListener("click", (ev) => {
    const item = ev.target.closest("button[data-i]");
    if (!item || item.disabled) return;
    const target = targets[Number(item.dataset.i)];
    closeMenu();
    target?.click();
  });
  attachMenuNav(menu, { close: closeMenu });
  // Closing with focus inside (Esc, Tab) hands it back to the ellipsis.
  menu.addEventListener("beforetoggle", (ev) => {
    if (ev.newState === "closed" && menu.contains(document.activeElement)) more.focus();
  });

  // The ribbon's box changes on window resize and when it's shown / hidden.
  const ro = new ResizeObserver(() => update());
  ro.observe(ribbon);
  update();

  return {
    detach() {
      ro.disconnect();
      closeMenu();
      more.remove();
      menu.remove();
      ribbon.classList.remove(MANAGED_CLASS);
      for (const el of ribbon.querySelectorAll(`:scope > .${OVERFLOWED_CLASS}`)) el.classList.remove(OVERFLOWED_CLASS);
    },
  };
}
