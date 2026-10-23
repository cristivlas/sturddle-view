// Phone portrait header (styles.css): About and Settings fold into a
// vertical-ellipsis menu whose items click the real (hidden) buttons.

import { mqMobilePortrait } from "./breakpoints.js";
import { attachMenuNav, focusFirstItem, mirrorItemsHtml, positionMenu, wireMenuToggle } from "./menu-nav.js";

const MORE_BTN_ID = "header-more-btn";
const MENU_ID = "header-more-menu";
const NO_POINTER = "none";

// A real button that can't take a click lists as disabled: its own state, or
// CSS pointer-events (Settings while disconnected, styles.css).
const isGated = (btn) => btn.disabled || getComputedStyle(btn).pointerEvents === NO_POINTER;

// `targets`: the header buttons the menu stands in for, in menu order.
export function wireHeaderMenu(targets) {
  const more = document.getElementById(MORE_BTN_ID);
  const menu = document.getElementById(MENU_ID);
  const closeMenu = () => { if (menu.matches(":popover-open")) menu.hidePopover(); };

  wireMenuToggle(more, menu, (ev) => {
    menu.innerHTML = mirrorItemsHtml(targets, isGated);
    menu.showPopover();
    positionMenu(menu, more);
    // Keyboard opens (detail 0) land on the first item; a tap leaves none
    // lit, since focus drives the item highlight.
    if (ev.detail === 0) focusFirstItem(menu);
  });
  // Re-gated at click: a disconnect can land while the menu is open.
  menu.addEventListener("click", (ev) => {
    const item = ev.target.closest("button[data-i]");
    const target = item && targets[Number(item.dataset.i)];
    if (!target || item.disabled || isGated(target)) return;
    closeMenu();
    target.click();
  });
  attachMenuNav(menu, { close: closeMenu });
  // Closing with focus inside (Esc, Tab) hands it back to the ellipsis.
  menu.addEventListener("beforetoggle", (ev) => {
    if (ev.newState === "closed" && menu.contains(document.activeElement)) more.focus();
  });
  // Rotating out of portrait hides the ellipsis: don't leave its menu behind.
  mqMobilePortrait.addEventListener("change", (ev) => { if (!ev.matches) closeMenu(); });
}
