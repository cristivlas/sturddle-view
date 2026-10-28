// Column header for split head/body tables (split-table.js): builds both
// tables and owns their column layout -- order, visibility, widths -- plus
// the resize, reorder, hide, fit and Columns-menu traits and the persisted
// layout. The body is the caller's: it fills the tbody and re-lays its rows
// out in onLayout(cols), which runs after every header rebuild.
//
// Column descriptor: { key, label, width, colClass?, thClass?, alwaysOn?,
// defaultHidden?, pinEnd? }. A pinEnd column stays last and shown; in px mode
// it is the auto-width column that takes the remaining space.

import { loadJson, saveJson } from "./storage.js";
import { attachColumnResize } from "./col-resize.js";
import { ARROW_CLASS, ARROW_DESC, TH_LABEL_CLASS, TH_SORTABLE_CLASS } from "./col-sort.js";
import { ESCAPE_KEY } from "./keys.js";
import { attachMenuNav, focusFirstItem, positionMenu, wireMenuToggle } from "./menu-nav.js";
import { applySplitColWidths, wireSplitScroll } from "./split-table.js";
import { escapeHtml } from "./wb-utils.js";

export const WIDTH_UNIT = Object.freeze({ PCT: "pct", PX: "px" });

const MENU_ICON = "sliders";
const MENU_BTN_LABEL = "Columns";
const FIT_LABEL = "Auto-fit";
const RESET_LABEL = "Reset";
const UP_ICON = "arrow-up-long";
const DOWN_ICON = "arrow-down-long";
const ACTIVE_CLASS = "tmb-active";
const MOVE = Object.freeze({ UP: -1, DOWN: 1 });
const MOVE_LABEL = Object.freeze({ [MOVE.UP]: "Move up:", [MOVE.DOWN]: "Move down:" });
const MENU_ACTION = Object.freeze({ TOGGLE: "toggle", FIT: "fit", RESET: "reset" });
const ENABLED_BTN = "button:not(:disabled)";
// Columns menu: a column's row, and the stable focus id on each control.
const MENU_COL_CLASS = "dt-menu-col";
const FID_ATTR = "data-fid";
const MENU_CONTROL_SEL = `button[${FID_ATTR}]`;
// Shared head-table class for factory-wide header CSS (styles.css).
const HEAD_TBL_CLASS = "dt-head-tbl";
const GRIP_CLASS = "th-grip";
const MENU_BTN_CLASS = "dt-menu-btn";
// Header drag-to-reorder (styles.css): the dragged th, the head table while
// a drag runs (grabbing cursor), the drop marker, and the cell's floating copy.
const DRAGGING_CLASS = "dt-th-dragging";
const REORDERING_CLASS = "dt-reordering";
const DROP_MARKER_CLASS = "dt-drop-marker";
const DRAG_GHOST_CLASS = "dt-drag-ghost";
// Presses here belong to the resize grip or the columns-menu button.
const REORDER_IGNORE_SEL = `.${GRIP_CLASS}, button`;
// Header-cell controls left out of the drag copy.
const GHOST_STRIP_SEL = `.${GRIP_CLASS}, .${MENU_BTN_CLASS}`;
const MOUSE_POINTER = "mouse";
// Sideways travel before a header press becomes a drag (a shorter one stays
// a click, which sorts).
const DRAG_THRESHOLD_PX = 5;
const FULL_PCT = 100;
// px mode: narrowest a fixed column can be dragged.
const MIN_COL_PX = 30;
const AUTO_WIDTH = "auto";
const FULL_WIDTH = "100%";

// Live instances per layoutKey (sync option): a layout change in one
// re-lays out the others from the saved layout.
const syncRegistry = new Map(); // layoutKey -> Set<reload fn>

// Saved { unit, order, hidden, widths } fit to the current columns. Widths in
// another unit (none = pct) drop; without reorder the declared order wins,
// so a varying column list (one per engine) doesn't append new keys last.
function reconcileLayout(columns, saved, fixedOrder, unit) {
  const byKey = new Map(columns.map((c) => [c.key, c]));
  const known = Array.isArray(saved.order) ? saved.order : [];
  const order = (fixedOrder ? [] : known).filter((k) => byKey.has(k));
  for (const c of columns) if (!order.includes(c.key)) order.push(c.key);
  const pinned = order.filter((k) => byKey.get(k).pinEnd);
  const ordered = order.filter((k) => !byKey.get(k).pinEnd).concat(pinned);
  const locked = (c) => c.alwaysOn || c.pinEnd;
  const hidden = new Set((Array.isArray(saved.hidden) ? saved.hidden : [])
    .filter((k) => byKey.has(k) && !locked(byKey.get(k))));
  for (const c of columns) if (c.defaultHidden && !locked(c) && !known.includes(c.key)) hidden.add(c.key);
  if (hidden.size === columns.length) hidden.clear();
  const savedWidths = (saved.unit ?? WIDTH_UNIT.PCT) === unit ? saved.widths : null;
  const widths = {};
  for (const c of columns) {
    const w = savedWidths?.[c.key];
    widths[c.key] = Number.isFinite(w) && w > 0 ? w : c.width;
  }
  return { order: ordered, hidden, widths };
}

// Column widths (% of the body) from each column's natural (max-content) px
// width. All fit: every column gets its natural width plus an equal share of
// the spare, so the gaps between contents are even. Overflow: columns no
// wider than an even share of the space keep their natural width
// (water-filling), and the wider ones split what's left evenly. Every
// column gets at least minPct; too many columns for that floor: equal shares.
export function fitPcts(natural, available, minPct) {
  const n = natural.length;
  if (minPct * n >= FULL_PCT || available <= 0) return natural.map(() => FULL_PCT / n);
  const floor = (minPct / FULL_PCT) * available;
  const px = natural.map((w) => Math.max(w, floor));
  const total = px.reduce((s, w) => s + w, 0);
  if (total <= available) {
    const extra = (available - total) / n;
    return px.map((w) => ((w + extra) / available) * FULL_PCT);
  }
  let free = px.map((_, i) => i);
  let budget = available;
  for (;;) {
    const share = budget / free.length;
    const fits = free.filter((i) => px[i] <= share);
    if (!fits.length) {
      // Each step keeps share >= floor (available >= n * floor).
      free.forEach((i) => { px[i] = share; });
      break;
    }
    fits.forEach((i) => { budget -= px[i]; });
    free = free.filter((i) => px[i] > share);
  }
  return px.map((w) => (w / available) * FULL_PCT);
}

const weightSum = (layout, cols) => cols.reduce((s, c) => s + layout.widths[c.key], 0) || 1;

// Store visible-column percentages back as weights on the visible columns'
// current scale, so hidden columns' weights stay comparable.
function storeVisiblePcts(layout, cols, pcts) {
  const total = weightSum(layout, cols);
  cols.forEach((c, i) => { layout.widths[c.key] = (pcts[i] * total) / FULL_PCT; });
}

// Index `key` (at order[i]) moves to: a shown column skips hidden neighbors
// so every move is visible in the table; a hidden one steps by one. A
// pinEnd column neither moves nor is passed. -1 = none.
function moveTarget(layout, byKey, i, dir) {
  if (byKey.get(layout.order[i]).pinEnd) return -1;
  const skipHidden = !layout.hidden.has(layout.order[i]);
  for (let j = i + dir; j >= 0 && j < layout.order.length; j += dir) {
    if (byKey.get(layout.order[j]).pinEnd) return -1;
    if (!skipHidden || !layout.hidden.has(layout.order[j])) return j;
  }
  return -1;
}

function moveColumn(layout, byKey, key, dir) {
  const i = layout.order.indexOf(key);
  const j = moveTarget(layout, byKey, i, dir);
  if (j < 0) return;
  layout.order.splice(i, 1);
  layout.order.splice(j, 0, key);
}

// Move visible column `from` to visible boundary `slot` (0 = first, n =
// after the last); hidden columns keep their place among their neighbors.
function dropColumn(layout, cols, from, slot) {
  const key = cols[from].key;
  layout.order.splice(layout.order.indexOf(key), 1);
  const at = slot < cols.length
    ? layout.order.indexOf(cols[slot].key)
    : layout.order.indexOf(cols[cols.length - 1].key) + 1;
  layout.order.splice(at, 0, key);
}

function toggleColumn(layout, key, visibleCount) {
  if (layout.hidden.has(key)) layout.hidden.delete(key);
  else if (visibleCount > 1) layout.hidden.add(key);
}

// One button: `fid` identifies it across rebuilds so focus can be restored.
function menuBtnHtml(fid, cls, inner, { disabled = false, attrs = "" } = {}) {
  return `<button class="${cls}" ${FID_ATTR}="${escapeHtml(fid)}"${attrs}${disabled ? " disabled" : ""}>${inner}</button>`;
}

function menuHtml(layout, byKey, traits, visibleCount) {
  const items = layout.order.map((key, i) => {
    const col = byKey.get(key);
    const shown = !layout.hidden.has(key);
    const lockToggle = !traits.hide || col.alwaysOn || col.pinEnd || (shown && visibleCount === 1);
    const move = (dir, icon) => {
      const label = escapeHtml(`${MOVE_LABEL[dir]} ${col.label || key}`);
      return menuBtnHtml(`${key}:${dir}`, "dt-menu-move", `<wa-icon name="${icon}"></wa-icon>`,
        { disabled: moveTarget(layout, byKey, i, dir) < 0, attrs: ` data-dir="${dir}" aria-label="${label}" title="${label}"` });
    };
    const moves = traits.reorder && !col.pinEnd ? move(MOVE.UP, UP_ICON) + move(MOVE.DOWN, DOWN_ICON) : "";
    const toggle = menuBtnHtml(`${key}:${MENU_ACTION.TOGGLE}`,
      `tmb-dd-item dt-menu-toggle${shown ? ` ${ACTIVE_CLASS}` : ""}`, escapeHtml(col.label),
      { disabled: lockToggle, attrs: ` data-action="${MENU_ACTION.TOGGLE}"` });
    return `<li class="${MENU_COL_CLASS}" data-key="${escapeHtml(key)}">${toggle}${moves}</li>`;
  }).join("");
  const action = (name, label) =>
    `<li>${menuBtnHtml(name, "tmb-dd-item", label, { attrs: ` data-action="${name}"` })}</li>`;
  return items + `<li class="tmb-separator"></li>` +
    (traits.fit ? action(MENU_ACTION.FIT, FIT_LABEL) : "") + action(MENU_ACTION.RESET, RESET_LABEL);
}

// Visible boundary nearest x: 0 = before the first column, n = after the
// last; never past `maxSlot` (ahead of a pinEnd column).
function dropSlot(ths, x, maxSlot) {
  let slot = 0;
  ths.forEach((th, i) => {
    const r = th.getBoundingClientRect();
    if (x > r.left + r.width / 2) slot = i + 1;
  });
  return Math.min(slot, maxSlot);
}

// Swallow the click a drag's release produces, ahead of the th's sort
// handler; the next press on `el` disarms it if no click came.
function swallowNextClick(el) {
  const eat = (ev) => { ev.stopPropagation(); ev.preventDefault(); disarm(); };
  const disarm = () => {
    el.removeEventListener("click", eat, true);
    el.removeEventListener("pointerdown", disarm, true);
  };
  el.addEventListener("click", eat, true);
  el.addEventListener("pointerdown", disarm, true);
}

// A floating copy of `th` that follows the drag: a one-cell table carrying
// the head table's classes, so the header CSS styles it the same.
function makeDragGhost(th, headTable) {
  const ghost = document.createElement("table");
  ghost.className = `${headTable.className} ${DRAG_GHOST_CLASS}`;
  ghost.inert = true;
  const cell = th.cloneNode(true);
  cell.querySelectorAll(GHOST_STRIP_SEL).forEach((el) => el.remove());
  ghost.createTHead().insertRow().append(cell);
  const r = th.getBoundingClientRect();
  Object.assign(ghost.style, { width: `${r.width}px`, height: `${r.height}px` });
  return ghost;
}

// Line the (placed) copy's label up with the original's: the header row's
// height and baseline come from the menu button, which copies lack.
function alignGhostLabel(ghost, th) {
  const labelTop = (el) => el.querySelector(`.${TH_LABEL_CLASS}`).getBoundingClientRect().top;
  const cell = ghost.tHead.rows[0].cells[0];
  const dy = labelTop(th) - labelTop(ghost);
  cell.style.paddingTop = `${parseFloat(getComputedStyle(cell).paddingTop) + dy}px`;
}

// reorder trait, mouse: drag a header cell sideways; past DRAG_THRESHOLD_PX
// a copy of the cell follows the pointer along the header, a marker shows
// the drop boundary, and the release calls onDrop(from, slot) (see
// dropColumn). Esc cancels. pinEnd cells don't drag.
function attachHeaderReorder(headTable, overlayHost, cols, onDrop) {
  const ths = Array.from(headTable.tHead.rows[0].cells);
  const maxSlot = cols.filter((c) => !c.pinEnd).length;
  ths.forEach((th, from) => {
    if (cols[from].pinEnd) return;
    th.addEventListener("pointerdown", (eDown) => {
      if (eDown.pointerType !== MOUSE_POINTER || eDown.button !== 0) return;
      if (eDown.target.closest(REORDER_IGNORE_SEL)) return;
      let marker = null;
      let ghost = null;
      let thLeft = 0;
      let slot = from;

      // Ghost: the cell's start position plus the pointer's travel, kept
      // within the header. Marker: on the drop boundary.
      function place(clientX) {
        const host = overlayHost.getBoundingClientRect();
        const head = headTable.getBoundingClientRect();
        const top = `${head.top - host.top}px`;
        const ghostLeft = Math.min(Math.max(thLeft + clientX - eDown.clientX, head.left),
          head.right - ghost.offsetWidth);
        Object.assign(ghost.style, { left: `${ghostLeft - host.left}px`, top });
        const x = slot < ths.length
          ? ths[slot].getBoundingClientRect().left
          : ths[ths.length - 1].getBoundingClientRect().right;
        Object.assign(marker.style, { left: `${x - host.left}px`, top, height: `${head.height}px` });
      }
      function startDrag() {
        thLeft = th.getBoundingClientRect().left;
        ghost = makeDragGhost(th, headTable);
        marker = document.createElement("div");
        marker.className = DROP_MARKER_CLASS;
        overlayHost.append(marker, ghost);
        place(eDown.clientX);
        alignGhostLabel(ghost, th);
        th.classList.add(DRAGGING_CLASS);
        headTable.classList.add(REORDERING_CLASS);
        // The th keeps the pointer (and its grabbing cursor) off-cell; losing
        // it without a release (e.g. the window loses focus) cancels.
        th.setPointerCapture(eDown.pointerId);
        th.addEventListener("lostpointercapture", onCancel);
      }
      function onMove(ev) {
        if (!marker) {
          if (Math.abs(ev.clientX - eDown.clientX) < DRAG_THRESHOLD_PX) return;
          startDrag();
        }
        slot = dropSlot(ths, ev.clientX, maxSlot);
        place(ev.clientX);
      }
      function end(drop) {
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onCancel);
        document.removeEventListener("keydown", onKey, true);
        th.removeEventListener("lostpointercapture", onCancel);
        if (!marker) return;
        marker.remove();
        marker = null;
        ghost.remove();
        th.classList.remove(DRAGGING_CLASS);
        headTable.classList.remove(REORDERING_CLASS);
        swallowNextClick(headTable);
        if (drop && slot !== from && slot !== from + 1) onDrop(from, slot);
      }
      const onUp = () => end(true);
      const onCancel = () => end(false);
      function onKey(ev) {
        if (ev.key !== ESCAPE_KEY || !marker) return;
        ev.preventDefault();
        ev.stopPropagation();
        end(false);
      }
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
      document.addEventListener("pointercancel", onCancel);
      document.addEventListener("keydown", onKey, true);
    });
  });
}

// The Columns button shares the first column's cell, which shows its width
// on top of the stored size: x px, or an x% share taken evenly from every
// column. show: stored -> shown sizes; store: back.
const BUTTON_ROOM = Object.freeze({
  [WIDTH_UNIT.PX]: {
    show: (sizes, x) => sizes.map((w, i) => (i ? w : w + x)),
    store: (sizes, x) => sizes.map((w, i) => (i ? w : w - x)),
  },
  [WIDTH_UNIT.PCT]: {
    show: (sizes, x) => sizes.map((w, i) => (w * (FULL_PCT - x)) / FULL_PCT + (i ? 0 : x)),
    store: (sizes, x) => sizes.map((w, i) => ((w - (i ? 0 : x)) * FULL_PCT) / (FULL_PCT - x)),
  },
});

// pct mode: a drag moves one boundary, each side floored at minAt(i) (the
// deficit taken from the neighbor), then every colgroup gets every width.
function makePctApplySizes(colElsLists, minAt) {
  return function applySizes(sizes, rctx) {
    if (rctx) {
      const { deltaFrac, startSizes, gripIdx } = rctx;
      const dPct = deltaFrac * FULL_PCT;
      const minA = minAt(gripIdx);
      const minB = minAt(gripIdx + 1);
      let a = startSizes[gripIdx] + dPct;
      let b = startSizes[gripIdx + 1] - dPct;
      if (a < minA) { b -= minA - a; a = minA; }
      if (b < minB) { a -= minB - b; b = minB; }
      sizes[gripIdx] = a;
      sizes[gripIdx + 1] = b;
    }
    applySplitColWidths(colElsLists, sizes);
  };
}

// px mode: fixed columns in px, floored at minAt(i) on a drag; the pinEnd
// column auto; both tables at least as wide as the fixed ones (`sizes`).
function makePxApplySizes(colElsLists, tables, minAt) {
  return function applySizes(sizes, rctx) {
    if (rctx) {
      const { deltaFrac, tableWidth, startSizes, gripIdx } = rctx;
      const d = deltaFrac * tableWidth;
      sizes[gripIdx] = Math.max(minAt(gripIdx), startSizes[gripIdx] + d);
      if (gripIdx + 1 < sizes.length) {
        sizes[gripIdx + 1] = Math.max(minAt(gripIdx + 1), startSizes[gripIdx + 1] - d);
      }
    }
    applySplitColWidths(colElsLists.map((els) => els.slice(0, sizes.length)), sizes, WIDTH_UNIT.PX);
    for (const els of colElsLists) els.slice(sizes.length).forEach((c) => { c.style.width = AUTO_WIDTH; });
    const fixedW = sizes.reduce((s, w) => s + w, 0);
    for (const t of tables) Object.assign(t.style, { width: FULL_WIDTH, minWidth: `${fixedW}px` });
  };
}

// The Columns button's px with its margin; 0 when absent or not laid out
// (detached or hidden: no box, and a detached margin reads "").
function menuButtonPx(thead) {
  const btn = thead.querySelector(`.${MENU_BTN_CLASS}`);
  const w = btn?.getBoundingClientRect().width;
  return w ? w + (parseFloat(getComputedStyle(btn).marginRight) || 0) : 0;
}

// Each shown column's max-content width (header + rows), measured on an
// offscreen auto-layout copy carrying both tables' classes.
function naturalWidths({ wrap, headTable, bodyTable, thead, tbody }) {
  const probe = document.createElement("table");
  probe.className = `${headTable.className} ${bodyTable.className}`;
  Object.assign(probe.style, {
    position: "absolute", visibility: "hidden", left: "0", top: "0",
    tableLayout: "auto", width: "max-content",
  });
  probe.append(thead.cloneNode(true), tbody.cloneNode(true));
  // Full-width rows would spread their text across the columns.
  for (const td of probe.querySelectorAll("tbody td[colspan]")) td.parentElement.remove();
  // Room for a sort arrow on every sortable header, not just the one sorted
  // now, so sorting another column later doesn't truncate its label.
  for (const th of probe.querySelectorAll(`th.${TH_SORTABLE_CLASS}:not(:has(.${ARROW_CLASS}))`)) {
    const arrow = document.createElement("wa-icon");
    arrow.className = ARROW_CLASS;
    arrow.setAttribute("name", ARROW_DESC);
    th.append(arrow);
  }
  wrap.append(probe);
  const natural = Array.from(probe.rows[0].cells, (c) => c.getBoundingClientRect().width);
  probe.remove();
  return natural;
}

// The Columns menu: a top-layer popover (escapes the scrollers' and WinBox's
// clip) rebuilt from html() on open. A control's click runs onAction({
// action, key, dir }), which re-lays out the table; the menu then refocuses.
function createColumnsMenu({ wrap, thead, html, onAction }) {
  const menu = document.createElement("ul");
  menu.className = "tmb-dropdown dt-menu";
  menu.setAttribute("popover", "auto");

  // Rebuilt in place on every change; the caller restores focus.
  const render = () => { menu.innerHTML = html(); };
  const isOpen = () => menu.matches(":popover-open");

  // The beforetoggle handler below returns focus to the button.
  function close() {
    if (isOpen()) menu.hidePopover();
  }

  // Focus `el`, or (disabled, e.g. an arrow at the end) its row's next
  // enabled control.
  function focusControl(el) {
    const target = el && !el.disabled ? el : el?.closest("li").querySelector(ENABLED_BTN);
    if (target) target.focus();
    else focusFirstItem(menu);
  }

  function onClick(ev) {
    const btn = ev.target.closest(MENU_CONTROL_SEL);
    if (!btn || btn.disabled) return;
    const action = btn.dataset.action;
    const dir = btn.dataset.dir ? Number(btn.dataset.dir) : null;
    if (dir === null && !Object.values(MENU_ACTION).includes(action)) return;
    onAction({ action, key: btn.closest(`.${MENU_COL_CLASS}`)?.dataset.key, dir });
    // Fit and Reset are one-shot table commands: close so the result shows.
    if (action === MENU_ACTION.FIT || action === MENU_ACTION.RESET) {
      close();
      return;
    }
    render();
    // A key press refocuses the same control, so repeats keep moving the
    // column; a mouse click, what moved under the still pointer (as hover
    // would). Focus picks the row showing arrows (CSS).
    const under = ev.detail > 0
      && document.elementFromPoint(ev.clientX, ev.clientY)?.closest(MENU_CONTROL_SEL);
    focusControl(under && menu.contains(under)
      ? under : menu.querySelector(`[${FID_ATTR}="${CSS.escape(btn.dataset.fid)}"]`));
  }

  menu.addEventListener("click", onClick);
  attachMenuNav(menu, { close });
  // Closing with focus inside (Esc, Tab) would drop it to <body>: hand it
  // back to the button instead. (A light-dismiss click already moved focus.)
  menu.addEventListener("beforetoggle", (ev) => {
    if (ev.newState === "closed" && menu.contains(document.activeElement)) {
      thead.querySelector(`.${MENU_BTN_CLASS}`)?.focus();
    }
  });
  wrap.append(menu);

  return {
    // Opened and placed synchronously (no unpositioned first paint). The
    // click bubbles on (col-sort skips th controls), so document-level
    // listeners such as other menus' close-on-click still see it.
    wire(btn) {
      wireMenuToggle(btn, menu, () => {
        render();
        menu.showPopover();
        positionMenu(menu, btn);
        focusFirstItem(menu);
      });
    },
    // A layout change from elsewhere (a synced table) shows in an open menu.
    refresh() {
      if (isOpen()) render();
    },
  };
}

// The caller fills the tbody, then calls relayout() once its body is ready.
export function createColumnHeader({
  columns,
  layoutKey,
  classes,           // { wrap, headScroll, headTable, bodyScroll, bodyTable }
  unit = WIDTH_UNIT.PCT, // px needs a pinEnd column to fill the rest
  minPct,            // pct floor per column
  traits = {},       // { resize?, reorder?, hide?, fit? }
  dragLineHeight,    // see attachColumnResize
  onLayout,          // (shownCols) after every header rebuild
  onSizes,           // () after widths are applied, drags included
  sync = false,      // instances sharing layoutKey follow each other live
  migrate,           // (saved) => saved, adapting an older stored format
}) {
  const byKey = new Map(columns.map((c) => [c.key, c]));
  const loadLayout = () => {
    const raw = loadJson(layoutKey);
    const saved = (migrate ? migrate(raw) : raw) || {};
    return reconcileLayout(columns, saved, !traits.reorder, unit);
  };
  let layout = loadLayout();
  const hasMenu = traits.reorder || traits.hide || traits.fit;

  const wrap = document.createElement("div");
  wrap.className = classes.wrap;
  wrap.innerHTML = `
    <div class="${classes.headScroll}">
      <table class="${classes.headTable} ${HEAD_TBL_CLASS}"><colgroup></colgroup><thead></thead></table>
    </div>
    <div class="${classes.bodyScroll}">
      <table class="${classes.bodyTable}"><colgroup></colgroup><tbody></tbody></table>
    </div>
  `;
  const [headScroll, bodyScroll] = wrap.children;
  const headTable = headScroll.querySelector("table");
  const bodyTable = bodyScroll.querySelector("table");
  const headColgroup = headTable.querySelector("colgroup");
  const bodyColgroup = bodyTable.querySelector("colgroup");
  const thead = headTable.querySelector("thead");
  const tbody = bodyTable.querySelector("tbody");
  wireSplitScroll(headScroll, bodyScroll);

  let resizeCtrl = null;
  // The shown sizes (a resize drag mutates them in place before saving),
  // their applier, and the button room they include (see BUTTON_ROOM).
  let liveSizes = [];
  let applyLive = null;
  let buttonRoom = 0;
  const room = BUTTON_ROOM[unit];

  const visibleCols = () => layout.order.filter((k) => !layout.hidden.has(k)).map((k) => byKey.get(k));
  const saveLayout = () => {
    saveJson(layoutKey, {
      unit, order: layout.order, hidden: [...layout.hidden], widths: layout.widths,
    });
    if (!sync) return;
    for (const reload of syncRegistry.get(layoutKey)) if (reload !== reloadFromSaved) reload();
  };
  function reloadFromSaved() {
    layout = loadLayout();
    relayout();
  }
  if (sync) {
    if (!syncRegistry.has(layoutKey)) syncRegistry.set(layoutKey, new Set());
    syncRegistry.get(layoutKey).add(reloadFromSaved);
  }

  function thHtml(col, i, cols) {
    const btn = i === 0 && hasMenu
      ? `<button class="${MENU_BTN_CLASS}" title="${MENU_BTN_LABEL}" aria-label="${MENU_BTN_LABEL}"><wa-icon name="${MENU_ICON}"></wa-icon></button>`
      : "";
    const grip = traits.resize && i < cols.length - 1 ? `<span class="${GRIP_CLASS}"></span>` : "";
    const thCls = col.thClass ? ` class="${col.thClass}"` : "";
    return `<th${thCls}>${btn}<span class="${TH_LABEL_CLASS}">${escapeHtml(col.label)}</span>${grip}</th>`;
  }

  // The button room in this unit: px, or its share of the head table.
  function measureButtonRoom() {
    const btnPx = menuButtonPx(thead);
    if (unit === WIDTH_UNIT.PX) return btnPx;
    const tableW = headTable.getBoundingClientRect().width;
    return tableW > btnPx ? (btnPx / tableW) * FULL_PCT : 0;
  }

  // Unrendered at the first layout (detached), and a pct share drifts with
  // the table's width: re-measure on every resize of the head table.
  function refreshButtonRoom() {
    const next = measureButtonRoom();
    if (!liveSizes.length || next === buttonRoom) return;
    liveSizes.splice(0, liveSizes.length, ...room.show(room.store(liveSizes, buttonRoom), next));
    buttonRoom = next;
    applyLive(liveSizes);
  }
  const roomObserver = hasMenu ? new ResizeObserver(refreshButtonRoom) : null;
  roomObserver?.observe(headTable);
  // Floors per shown column; the first also keeps its button room.
  const minAt = (i) => (unit === WIDTH_UNIT.PX ? MIN_COL_PX : minPct) + (i ? 0 : buttonRoom);

  // Widths for the shown columns: pct of the table, or px for the fixed
  // ones (the pinEnd column is auto).
  function attachWidths(cols) {
    const colEls = [Array.from(headColgroup.children), Array.from(bodyColgroup.children)];
    const px = unit === WIDTH_UNIT.PX;
    const sized = px ? cols.filter((c) => !c.pinEnd) : cols;
    const total = weightSum(layout, sized);
    const stored = sized.map((c) => (px ? layout.widths[c.key] : (layout.widths[c.key] / total) * FULL_PCT));
    buttonRoom = stored.length ? measureButtonRoom() : 0;
    const sizes = room.show(stored, buttonRoom);
    liveSizes = sizes;
    const apply = px ? makePxApplySizes(colEls, [headTable, bodyTable], minAt) : makePctApplySizes(colEls, minAt);
    const applySizes = (s, rctx) => { apply(s, rctx); onSizes?.(); };
    applyLive = applySizes;
    resizeCtrl?.destroy();
    resizeCtrl = null;
    if (!traits.resize) {
      applySizes(sizes);
      return;
    }
    resizeCtrl = attachColumnResize({
      table: headTable,
      grips: Array.from(thead.querySelectorAll(`.${GRIP_CLASS}`)).slice(0, sizes.length),
      overlayHost: wrap,
      sizes,
      unit,
      applySizes,
      dragLineHeight,
      onSave: (s) => {
        const kept = room.store(s, buttonRoom);
        if (px) sized.forEach((c, i) => { layout.widths[c.key] = kept[i]; });
        else storeVisiblePcts(layout, sized, kept);
        saveLayout();
      },
    });
  }

  // Rebuild header + colgroups for the current order/visibility, re-attach
  // resize, reorder and the menu button, then hand the body its new columns.
  function relayout() {
    const cols = visibleCols();
    const colsHtml = cols.map((c) => (c.colClass ? `<col class="${c.colClass}">` : "<col>")).join("");
    headColgroup.innerHTML = colsHtml;
    bodyColgroup.innerHTML = colsHtml;
    thead.innerHTML = `<tr>${cols.map(thHtml).join("")}</tr>`;
    const menuBtn = thead.querySelector(`.${MENU_BTN_CLASS}`);
    if (menuBtn) menu.wire(menuBtn);
    attachWidths(cols);
    if (traits.reorder) {
      attachHeaderReorder(headTable, wrap, cols, (from, slot) => {
        dropColumn(layout, cols, from, slot);
        saveLayout();
        relayout();
      });
    }
    menu?.refresh();
    onLayout?.(cols);
  }

  // Stored widths fit to content. The first column's measure includes the
  // Columns button, which its button room covers. px: each fixed column at
  // its natural width; pct: see fitPcts.
  function fitColumns() {
    const cols = visibleCols();
    const btnPx = menuButtonPx(thead);
    const natural = naturalWidths({ wrap, headTable, bodyTable, thead, tbody });
    natural[0] -= btnPx;
    if (unit === WIDTH_UNIT.PX) {
      cols.forEach((c, i) => { if (!c.pinEnd) layout.widths[c.key] = Math.max(MIN_COL_PX, natural[i]); });
      return;
    }
    const available = bodyTable.getBoundingClientRect().width - btnPx;
    storeVisiblePcts(layout, cols, fitPcts(natural, available, minPct));
  }

  function applyMenuAction({ action, key, dir }) {
    if (dir !== null) moveColumn(layout, byKey, key, dir);
    else if (action === MENU_ACTION.TOGGLE) toggleColumn(layout, key, visibleCols().length);
    else if (action === MENU_ACTION.FIT) fitColumns();
    else layout = reconcileLayout(columns, {}, !traits.reorder, unit);
    saveLayout();
    relayout();
  }

  const menu = hasMenu
    ? createColumnsMenu({
      wrap, thead, onAction: applyMenuAction,
      html: () => menuHtml(layout, byKey, traits, visibleCols().length),
    })
    : null;

  return {
    wrap,
    headTable,
    bodyTable,
    thead,
    tbody,
    bodyScroll,
    relayout,
    // Summed px of the shown non-pinEnd columns (px mode), live mid-drag.
    fixedWidth: () => liveSizes.reduce((s, w) => s + w, 0),
    destroy() {
      resizeCtrl?.destroy();
      roomObserver?.disconnect();
      if (!sync) return;
      const set = syncRegistry.get(layoutKey);
      set.delete(reloadFromSaved);
      if (!set.size) syncRegistry.delete(layoutKey);
    },
  };
}
