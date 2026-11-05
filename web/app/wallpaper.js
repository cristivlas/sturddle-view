// Wallpaper: one global image (server-stored), shared by every view that
// mounts it, painted behind a view's region on desktop. Picked from the
// user's machine through a ribbon button's menu (Choose / Remove, Fit, Fade),
// also opened by right-clicking the region's bare background. Content above
// it simply covers it.

import { mqMobile } from "./breakpoints.js";
import { reportError } from "./dialogs.js";
import { attachMenuNav, focusFirstItem, MENU_CHECKED_CLASS, positionMenu, positionMenuAt, wireMenuToggle } from "./menu-nav.js";
import { ribbonAnchor } from "./ribbon-overflow.js";
import { escapeHtml } from "./wb-utils.js";

const WALLPAPER_PATH = "/wallpaper";
const INFO_PATH = `${WALLPAPER_PATH}/info`;
const STYLE_PATH = `${WALLPAPER_PATH}/style`;
const ACCEPT_TYPES = "image/png,image/jpeg,image/webp";
const HAS_CLASS = "has-wallpaper";
const URL_VAR = "--wallpaper";
const FADE_VAR = "--wallpaper-fade";
const FIT_ATTR = "data-wallpaper-fit";
// On the menu when it sits in the viewport's right half: flyouts open leftward.
const FLYOUT_LEFT_CLASS = "flyout-left";
const BTN_LABEL = "Wallpaper";
const ICON = "image";
const ITEM = Object.freeze({ CHOOSE: "choose", REMOVE: "remove" });
const ITEM_LABEL = Object.freeze({ [ITEM.CHOOSE]: "Choose image...", [ITEM.REMOVE]: "Remove" });
const FIT_LABEL = "Fit";
const FADE_LABEL = "Fade";
const FADE_STEP_PCT = 5;
const PCT_SUFFIX = "%";
const SET_FAIL_MSG = "Set wallpaper failed";
const REMOVE_FAIL_MSG = "Remove wallpaper failed";
const LOAD_FAIL_MSG = "Load wallpaper failed";
const STYLE_FAIL_MSG = "Save wallpaper style failed";

// Last info any mount loaded: a view switch paints at once, then refreshes.
let cachedInfo = null;

const capitalize = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// Raw-body PUT (the shared api() helper sends JSON only); throws the same
// error shape so reportError renders it alike.
async function uploadImage(file) {
  const r = await fetch(WALLPAPER_PATH, {
    method: "PUT", headers: { "Content-Type": file.type }, body: file,
  });
  if (!r.ok) throw new Error(`PUT ${WALLPAPER_PATH} -> ${r.status} ${await r.text()}`);
  return r.json();
}

// A submenu row: trigger button plus its flyout list. Disabled without an
// image, since its settings would show nothing (styles.css keeps it shut).
function submenuHtml(label, inner, info) {
  const disabled = info.version == null ? " disabled" : "";
  return `<li class="tmb-dd-submenu"><button type="button" class="tmb-dd-item"${disabled}>${label}</button>`
    + `<ul class="tmb-dropdown">${inner}</ul></li>`;
}

function itemHtml(item, disabled) {
  return `<li><button type="button" class="tmb-dd-item" data-item="${item}"${disabled ? " disabled" : ""}>`
    + `${ITEM_LABEL[item]}</button></li>`;
}

// Fit submenu, the current fit checked.
function fitHtml(info) {
  const opts = info.fits.map((f) => {
    const cls = f === info.fit ? ` ${MENU_CHECKED_CLASS}` : "";
    return `<li><button type="button" class="tmb-dd-item${cls}" data-fit="${escapeHtml(f)}">`
      + `${escapeHtml(capitalize(f))}</button></li>`;
  }).join("");
  return submenuHtml(FIT_LABEL, opts, info);
}

// Fade flyout: the slider and its live value.
function fadeHtml(info) {
  return submenuHtml(FADE_LABEL, `<li class="wallpaper-fade">`
    + `<wa-slider size="small" min="0" max="${info.fade_max}" step="${FADE_STEP_PCT}" value="${info.fade}"></wa-slider>`
    + `<span class="wallpaper-fade-val">${info.fade}${PCT_SUFFIX}</span></li>`, info);
}

function ribbonButton(ribbonEl) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ribbon-btn wallpaper-btn";
  btn.setAttribute("aria-label", BTN_LABEL);
  btn.title = BTN_LABEL;
  btn.setAttribute("aria-haspopup", "menu");
  btn.innerHTML = `<wa-icon name="${ICON}"></wa-icon>`;
  btn.disabled = true;
  const sep = document.createElement("span");
  sep.className = "ribbon-sep";
  sep.setAttribute("aria-hidden", "true");
  ribbonEl.append(sep, btn);
  return { btn, sep };
}

// Mount a button (last on each of ribbonEls, after a separator) sharing one
// menu, and paint the stored wallpaper on regionEl. A right-click on regionEl
// opens the menu where isBare(target) holds. Any of mobileQueries matching
// is the view's mobile layout. Returns { unmount }.
export function mountWallpaper({ ribbonEls, regionEl, isBare, mobileQueries = [mqMobile], api, log }) {
  const errCtx = { log };
  const isMobile = () => mobileQueries.some((mq) => mq.matches);
  let info = cachedInfo;
  // A request settling after unmount must not repaint the (shared) region.
  let mounted = true;
  // Where focus returns when the menu closes.
  let returnFocus = null;

  const buttons = ribbonEls.map(ribbonButton);
  const menu = document.createElement("ul");
  menu.className = "tmb-dropdown wallpaper-menu";
  menu.setAttribute("popover", "auto");
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ACCEPT_TYPES;
  input.hidden = true;
  regionEl.append(menu, input);

  const closeMenu = () => { if (menu.matches(":popover-open")) menu.hidePopover(); };
  const setFade = (pct) => regionEl.style.setProperty(FADE_VAR, `${pct}${PCT_SUFFIX}`);
  const clearPaint = () => {
    regionEl.classList.remove(HAS_CLASS);
    regionEl.style.removeProperty(URL_VAR);
    regionEl.style.removeProperty(FADE_VAR);
    regionEl.removeAttribute(FIT_ATTR);
  };

  // Desktop only: phones get neither the image nor the buttons.
  const paint = () => {
    if (!mounted) return;
    const mobile = isMobile();
    if (info?.version != null && !mobile) {
      regionEl.classList.add(HAS_CLASS);
      regionEl.style.setProperty(URL_VAR, `url("${WALLPAPER_PATH}?v=${info.version}")`);
      regionEl.setAttribute(FIT_ATTR, info.fit);
      setFade(info.fade);
    } else {
      clearPaint();
    }
    // Disabled (not hidden) until info loads: showing it later wouldn't
    // resize the ribbon, so its overflow wouldn't re-fit.
    for (const { btn, sep } of buttons) {
      btn.disabled = !info;
      btn.hidden = sep.hidden = mobile;
    }
    if (mobile) closeMenu();
  };
  const apply = (next) => { info = cachedInfo = next; paint(); };
  // A failed save repaints the stored style and closes the menu, so a reopen
  // renders the slider from it too.
  const putStyle = (patch) => api("PUT", STYLE_PATH, patch).then(apply, (e) => {
    paint();
    closeMenu();
    reportError(errCtx, STYLE_FAIL_MSG, e);
  });

  // Render and show the menu; place(menu) positions it once laid out.
  const openMenu = (place, keyboard) => {
    menu.innerHTML = itemHtml(ITEM.CHOOSE, false) + itemHtml(ITEM.REMOVE, info.version == null)
      + `<li class="tmb-separator"></li>` + fitHtml(info) + fadeHtml(info);
    const slider = menu.querySelector("wa-slider");
    const val = menu.querySelector(".wallpaper-fade-val");
    slider.addEventListener("input", () => {
      setFade(slider.value);
      val.textContent = `${slider.value}${PCT_SUFFIX}`;
    });
    slider.addEventListener("change", () => putStyle({ fade: Number(slider.value) }));
    menu.classList.remove(FLYOUT_LEFT_CLASS);
    menu.showPopover();
    place(menu);
    const r = menu.getBoundingClientRect();
    menu.classList.toggle(FLYOUT_LEFT_CLASS, r.left + r.width / 2 > window.innerWidth / 2);
    if (keyboard) focusFirstItem(menu);
  };
  for (const { btn } of buttons) {
    wireMenuToggle(btn, menu, (ev) => {
      returnFocus = ribbonAnchor(btn);
      openMenu((m) => positionMenu(m, returnFocus), ev.detail === 0);
    });
  }
  // The app already suppresses the native context menu everywhere.
  const onContextMenu = (ev) => {
    if (!info || isMobile() || menu.contains(ev.target) || !isBare(ev.target)) return;
    closeMenu();
    returnFocus = document.activeElement;
    openMenu((m) => positionMenuAt(m, { top: ev.clientY, bottom: ev.clientY, left: ev.clientX }), false);
  };
  regionEl.addEventListener("contextmenu", onContextMenu);
  menu.addEventListener("click", (ev) => {
    const item = ev.target.closest("button[data-item], button[data-fit]");
    if (!item || item.disabled) return;
    closeMenu();
    if (item.dataset.fit) {
      putStyle({ fit: item.dataset.fit });
    } else if (item.dataset.item === ITEM.CHOOSE) {
      input.click();
    } else {
      api("DELETE", WALLPAPER_PATH).then(apply, (e) => reportError(errCtx, REMOVE_FAIL_MSG, e));
    }
  });
  attachMenuNav(menu, { close: closeMenu });
  menu.addEventListener("beforetoggle", (ev) => {
    if (ev.newState === "closed" && menu.contains(document.activeElement)) returnFocus?.focus();
  });
  input.addEventListener("change", () => {
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    uploadImage(file).then(apply, (e) => reportError(errCtx, SET_FAIL_MSG, e));
  });
  mobileQueries.forEach((mq) => mq.addEventListener("change", paint));

  paint();
  api("GET", INFO_PATH).then(apply, (e) => reportError(errCtx, LOAD_FAIL_MSG, e));

  return {
    unmount() {
      mounted = false;
      mobileQueries.forEach((mq) => mq.removeEventListener("change", paint));
      regionEl.removeEventListener("contextmenu", onContextMenu);
      closeMenu();
      clearPaint();
      for (const { btn, sep } of buttons) { btn.remove(); sep.remove(); }
      menu.remove(); input.remove();
    },
  };
}
