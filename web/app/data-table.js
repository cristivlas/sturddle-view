// Table factory: builds a split head/body table (split-table.js) from a
// column list and wires the requested traits -- sort, resize, reorder, hide,
// fit -- so a table declares its columns instead of re-wiring each helper.
//
// Column descriptor: { key, label, width (default %), cell(row) => html,
// tdClass?, tdTitle?(row) => text, alwaysOn?, plus the sort-stack.js fields
// (field, numeric, firstDir, tiebreak, sortable) }.

import { loadJson, saveJson } from "./storage.js";
import { attachColumnResize, makePctApplySizes } from "./col-resize.js";
import { attachLayeredSort, sortByStack } from "./sort-stack.js";
import { ARROW_CLASS, ARROW_DESC, attachColumnSort, TH_LABEL_CLASS, TH_SORTABLE_CLASS } from "./col-sort.js";
import { ESCAPE_KEY } from "./keys.js";
import { attachMenuNav, focusFirstItem } from "./menu-nav.js";
import { wireSplitScroll } from "./split-table.js";
import { escapeHtml, LIST_NAV_KEYS, wireArrowKeyNav } from "./wb-utils.js";

const MENU_ICON = "sliders";
const MENU_BTN_LABEL = "Columns";
const FIT_LABEL = "Fit columns";
const RESET_LABEL = "Reset columns";
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
// rowNav body markers (styles.css): the tab-stop body, plus cursor mode.
const ROWNAV_CLASS = "dt-rownav";
const ROWNAV_CURSOR_CLASS = "dt-rownav-cursor";
// Set while the arrows drive the body; styles.css gates row hover tints on it.
const KBD_CLASS = "dt-kbd";
// Navigable rows carry a data-id (group/empty rows don't); the current one
// is marked aria-selected.
const NAV_ROW_SEL = "tr[data-id]";
const CURRENT_ATTR = "aria-selected";
const CURRENT_ROW_SEL = `tr[${CURRENT_ATTR}="true"]`;
const GRID_ROLE = "grid";
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
const MENU_GAP_PX = 4;
const MENU_EDGE_PX = 8;
const FULL_PCT = 100;

// Why the body was rebuilt (createTable's onRender argument).
export const RENDER_REASON = Object.freeze({ DATA: "data", SORT: "sort", LAYOUT: "layout" });

// Saved { order, hidden, widths } reconciled with the current column list:
// unknown keys drop, new columns append, alwaysOn columns never hide. An
// empty `saved` yields the defaults. Widths are relative weights. Without the
// reorder trait the declared order always wins (a column list that varies,
// e.g. one column per engine, would otherwise append new keys at the end).
function reconcileLayout(columns, saved, fixedOrder) {
  const byKey = new Map(columns.map((c) => [c.key, c]));
  const savedOrder = !fixedOrder && Array.isArray(saved.order) ? saved.order : [];
  const order = savedOrder.filter((k) => byKey.has(k));
  for (const c of columns) if (!order.includes(c.key)) order.push(c.key);
  const hidden = new Set((Array.isArray(saved.hidden) ? saved.hidden : [])
    .filter((k) => byKey.has(k) && !byKey.get(k).alwaysOn));
  if (hidden.size === columns.length) hidden.clear();
  const widths = {};
  for (const c of columns) {
    const w = saved.widths?.[c.key];
    widths[c.key] = Number.isFinite(w) && w > 0 ? w : c.width;
  }
  return { order, hidden, widths };
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
// so every move is visible in the table; a hidden one steps by one. -1 = none.
function moveTarget(layout, i, dir) {
  const skipHidden = !layout.hidden.has(layout.order[i]);
  for (let j = i + dir; j >= 0 && j < layout.order.length; j += dir) {
    if (!skipHidden || !layout.hidden.has(layout.order[j])) return j;
  }
  return -1;
}

function moveColumn(layout, key, dir) {
  const i = layout.order.indexOf(key);
  const j = moveTarget(layout, i, dir);
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

function tdHtml(col, row) {
  const cls = col.tdClass ? ` class="${col.tdClass}"` : "";
  const titleText = col.tdTitle?.(row);
  const title = titleText ? ` title="${escapeHtml(titleText)}"` : "";
  return `<td${cls}${title}>${col.cell(row)}</td>`;
}

// <tr> open tag from the caller's rowAttrs(row, i) => { name: value } (class,
// data-id, tabindex, ...; i = display position). Null values are skipped.
function trOpen(row, i, rowAttrs) {
  const attrs = rowAttrs ? Object.entries(rowAttrs(row, i)).filter(([, v]) => v != null) : [];
  return `<tr${attrs.map(([k, v]) => ` ${k}="${escapeHtml(String(v))}"`).join("")}>`;
}

// One button: `fid` identifies it across rebuilds so focus can be restored.
function menuBtnHtml(fid, cls, inner, { disabled = false, attrs = "" } = {}) {
  return `<button class="${cls}" ${FID_ATTR}="${escapeHtml(fid)}"${attrs}${disabled ? " disabled" : ""}>${inner}</button>`;
}

function menuHtml(layout, byKey, traits, visibleCount) {
  const items = layout.order.map((key, i) => {
    const col = byKey.get(key);
    const shown = !layout.hidden.has(key);
    const lockToggle = !traits.hide || col.alwaysOn || (shown && visibleCount === 1);
    const move = (dir, icon) => {
      const label = escapeHtml(`${MOVE_LABEL[dir]} ${col.label || key}`);
      return menuBtnHtml(`${key}:${dir}`, "dt-menu-move", `<wa-icon name="${icon}"></wa-icon>`,
        { disabled: moveTarget(layout, i, dir) < 0, attrs: ` data-dir="${dir}" aria-label="${label}" title="${label}"` });
    };
    const moves = traits.reorder ? move(MOVE.UP, UP_ICON) + move(MOVE.DOWN, DOWN_ICON) : "";
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

// Below the button unless it only fits (or fits better) above; capped to
// the chosen side's room so a tall menu scrolls inside. Clamped inside the
// viewport horizontally.
function positionMenu(menu, btn) {
  const r = btn.getBoundingClientRect();
  menu.style.maxHeight = "";
  const h = menu.offsetHeight;
  const roomBelow = window.innerHeight - r.bottom - MENU_GAP_PX - MENU_EDGE_PX;
  const roomAbove = r.top - MENU_GAP_PX - MENU_EDGE_PX;
  const below = h <= roomBelow || roomBelow >= roomAbove;
  const room = Math.max(0, below ? roomBelow : roomAbove);
  menu.style.maxHeight = room + "px";
  menu.style.top = (below ? r.bottom + MENU_GAP_PX : r.top - MENU_GAP_PX - Math.min(h, room)) + "px";
  const maxLeft = window.innerWidth - menu.offsetWidth - MENU_EDGE_PX;
  menu.style.left = Math.max(MENU_EDGE_PX, Math.min(r.left, maxLeft)) + "px";
}

// rowNav trait: the body table is one Tab stop and the arrows move the
// current row (wireArrowKeyNav), which rings while the body has keyboard
// focus. With isSelected(row), the current row is the caller's selection and
// arrows call onSelect(rowEl) / onExtend(rowEl) (Shift) from lead()?.
// Without it, the factory keeps a highlight-only cursor, which a row click
// also moves (so Enter after a click acts on the clicked row).
function attachRowNav(bodyTable, nav, cursor) {
  bodyTable.tabIndex = 0;
  // aria-selected marks the current row; rows support it inside a grid.
  bodyTable.setAttribute("role", GRID_ROLE);
  bodyTable.classList.add(ROWNAV_CLASS);
  if (!nav.isSelected) bodyTable.classList.add(ROWNAV_CURSOR_CLASS);
  const moveCursor = (row) => {
    bodyTable.querySelector(CURRENT_ROW_SEL)?.removeAttribute(CURRENT_ATTR);
    row.setAttribute(CURRENT_ATTR, "true");
    cursor.id = row.dataset.id;
  };
  wireArrowKeyNav(bodyTable, {
    rows: NAV_ROW_SEL,
    selected: CURRENT_ROW_SEL,
    lead: nav.lead,
    select: nav.isSelected ? nav.onSelect : moveCursor,
    extend: nav.onExtend,
  });
  // Arrow use hides row hover tints until the mouse moves again, so a
  // resting pointer never leaves a second highlight beside the current row.
  bodyTable.addEventListener("keydown", (ev) => {
    if (LIST_NAV_KEYS.has(ev.key)) bodyTable.classList.add(KBD_CLASS);
  });
  bodyTable.addEventListener("pointermove", () => bodyTable.classList.remove(KBD_CLASS));
  if (!nav.isSelected) {
    bodyTable.addEventListener("click", (ev) => {
      const row = ev.target.closest(NAV_ROW_SEL);
      if (row) moveCursor(row);
    });
  }
}

// Wrap rowAttrs to mark the current row (selected, or the cursor's data-id).
function rowNavAttrs(rowAttrs, nav, cursor) {
  return (r, i) => {
    const a = rowAttrs ? rowAttrs(r, i) : {};
    if (a["data-id"] == null) return a;
    const current = nav.isSelected ? nav.isSelected(r) : String(a["data-id"]) === cursor.id;
    return current ? { ...a, [CURRENT_ATTR]: "true" } : a;
  };
}

// Visible boundary nearest x: 0 = before the first column, n = after the last.
function dropSlot(ths, x) {
  let slot = 0;
  ths.forEach((th, i) => {
    const r = th.getBoundingClientRect();
    if (x > r.left + r.width / 2) slot = i + 1;
  });
  return slot;
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
// dropColumn). Esc cancels.
function attachHeaderReorder(headTable, overlayHost, onDrop) {
  const ths = Array.from(headTable.tHead.rows[0].cells);
  ths.forEach((th, from) => {
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
        slot = dropSlot(ths, ev.clientX);
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

// traits: { sort?: { sortKey, stackKey?, defaultState?, single?, group? },
// resize?, reorder?, hide?, fit?, rowNav? }; sort.single keeps one active
// column (no layered stack); sort.group(a, b) is a fixed partition applied
// first (see sortByStack); rowNav: see attachRowNav. `cls` prefixes the wrap/scroller/table classes; the caller
// appends `wrap` and fills it via render(rows). rowAttrs sets each <tr>'s
// attributes (see trOpen). fullRow(row) => { html, tdClass? } | null renders
// a row as one cell spanning every column (group headings, empty notes).
// onRender(reason) runs after every body rebuild (RENDER_REASON), so callers
// can repaint row state.
export function createTable({
  cls, tableClass, columns, layoutKey, minPct, traits = {}, rowAttrs, fullRow, onRender,
}) {
  const byKey = new Map(columns.map((c) => [c.key, c]));
  const layout = reconcileLayout(columns, loadJson(layoutKey) || {}, !traits.reorder);
  const hasMenu = traits.reorder || traits.hide || traits.fit;

  const wrap = document.createElement("div");
  wrap.className = `${cls}-table-wrap`;
  wrap.innerHTML = `
    <div class="${cls}-head-scroll">
      <table class="${tableClass} ${cls}-head-tbl ${HEAD_TBL_CLASS}"><colgroup></colgroup><thead></thead></table>
    </div>
    <div class="${cls}-body-scroll">
      <table class="${tableClass} ${cls}-body-tbl"><colgroup></colgroup><tbody></tbody></table>
    </div>
  `;
  const headTable = wrap.querySelector(`.${cls}-head-tbl`);
  const bodyTable = wrap.querySelector(`.${cls}-body-tbl`);
  const headColgroup = headTable.querySelector("colgroup");
  const bodyColgroup = bodyTable.querySelector("colgroup");
  const thead = headTable.querySelector("thead");
  const tbody = bodyTable.querySelector("tbody");
  wireSplitScroll(wrap.querySelector(`.${cls}-head-scroll`), wrap.querySelector(`.${cls}-body-scroll`));

  let rows = null;
  // Rows in rendered order (tbody.rows[i] <-> renderedRows[i]).
  let renderedRows = [];
  let sortCtrl = null;
  // Cursor-mode rowNav's current row, by data-id (survives re-renders).
  const cursor = { id: null };
  const attrsFor = traits.rowNav ? rowNavAttrs(rowAttrs, traits.rowNav, cursor) : rowAttrs;
  if (traits.rowNav) attachRowNav(bodyTable, traits.rowNav, cursor);
  let resizeCtrl = null;
  let menu = null;

  const visibleCols = () => layout.order.filter((k) => !layout.hidden.has(k)).map((k) => byKey.get(k));
  const saveLayout = () => saveJson(layoutKey, {
    order: layout.order, hidden: [...layout.hidden], widths: layout.widths,
  });

  // Hidden columns drop out of the sort (the arrow follows the next shown
  // entry); they stay in the persisted stack, so showing one restores it.
  function renderBody(reason) {
    if (!rows) return;
    const cols = visibleCols();
    const sorted = rows.slice();
    if (sortCtrl) {
      sortByStack(sorted, sortCtrl.stack().filter((s) => !layout.hidden.has(s.key)), columns, traits.sort.group);
    }
    renderedRows = sorted;
    tbody.innerHTML = sorted
      .map((r, i) => {
        const full = fullRow?.(r);
        const cells = full
          ? `<td colspan="${cols.length}"${full.tdClass ? ` class="${full.tdClass}"` : ""}>${full.html}</td>`
          : cols.map((c) => tdHtml(c, r)).join("");
        return `${trOpen(r, i, attrsFor)}${cells}</tr>`;
      })
      .join("");
    onRender?.(reason);
  }

  function thHtml(col, i, cols) {
    const btn = i === 0 && hasMenu
      ? `<button class="${MENU_BTN_CLASS}" title="${MENU_BTN_LABEL}" aria-label="${MENU_BTN_LABEL}"><wa-icon name="${MENU_ICON}"></wa-icon></button>`
      : "";
    const grip = traits.resize && i < cols.length - 1 ? `<span class="${GRIP_CLASS}"></span>` : "";
    return `<th>${btn}<span class="${TH_LABEL_CLASS}">${escapeHtml(col.label)}</span>${grip}</th>`;
  }

  // Rebuild header + colgroups for the current order/visibility, re-attach
  // resize and sort to the new ths, then re-render the body.
  function relayout() {
    const cols = visibleCols();
    const colsHtml = "<col>".repeat(cols.length);
    headColgroup.innerHTML = colsHtml;
    bodyColgroup.innerHTML = colsHtml;
    thead.innerHTML = `<tr>${cols.map(thHtml).join("")}</tr>`;
    const menuBtn = thead.querySelector(`.${MENU_BTN_CLASS}`);
    if (menuBtn) wireMenuButton(menuBtn);

    const total = weightSum(layout, cols);
    const sizes = cols.map((c) => (layout.widths[c.key] / total) * FULL_PCT);
    const applySizes = makePctApplySizes(
      [Array.from(headColgroup.children), Array.from(bodyColgroup.children)], minPct);
    resizeCtrl?.destroy();
    resizeCtrl = null;
    if (traits.resize) {
      resizeCtrl = attachColumnResize({
        table: headTable,
        grips: Array.from(thead.querySelectorAll(`.${GRIP_CLASS}`)),
        overlayHost: wrap,
        sizes,
        unit: "pct",
        applySizes,
        onSave: (s) => { storeVisiblePcts(layout, cols, s); saveLayout(); },
      });
    } else {
      applySizes(sizes);
    }
    if (traits.sort) sortCtrl = attachSort(cols);
    if (traits.reorder) {
      attachHeaderReorder(headTable, wrap, (from, slot) => {
        dropColumn(layout, cols, from, slot);
        saveLayout();
        relayout();
      });
    }
    renderBody(RENDER_REASON.LAYOUT);
  }

  // Header sort: layered (MRU stack, sort-stack.js) unless sort.single (one
  // active column, col-sort.js). Either way the body sorts by a stack, and
  // getSort/setSort let the caller read and drive it (e.g. ribbon buttons).
  function attachSort(cols) {
    const { single, ...opts } = traits.sort;
    const onChange = () => renderBody(RENDER_REASON.SORT);
    if (single) {
      const ctrl = attachColumnSort({
        table: headTable, columns: cols, storageKey: opts.sortKey, keyboard: true, onSort: onChange,
      });
      const stack = () => { const c = ctrl.current(); return c ? [{ key: c.key, dir: c.dir }] : []; };
      return { stack, set: ctrl.set };
    }
    const ctrl = attachLayeredSort({ table: headTable, columns: cols, ...opts, keyboard: true, onChange });
    return { stack: ctrl.get, set: ctrl.set };
  }

  // Measure each visible column's max-content width (header + rows) on an
  // offscreen auto-layout copy carrying both tables' classes, then share
  // 100% in proportion.
  function fitColumns() {
    const cols = visibleCols();
    const probe = document.createElement("table");
    probe.className = `${tableClass} ${cls}-head-tbl ${HEAD_TBL_CLASS} ${cls}-body-tbl`;
    Object.assign(probe.style, {
      position: "absolute", visibility: "hidden", left: "0", top: "0",
      tableLayout: "auto", width: "max-content",
    });
    probe.append(thead.cloneNode(true), tbody.cloneNode(true));
    // Full-width rows (fullRow) would spread their text across the columns.
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
    storeVisiblePcts(layout, cols, fitPcts(natural, bodyTable.getBoundingClientRect().width, minPct));
  }

  // Rebuilt in place on every change; the caller restores focus.
  function renderMenu() {
    menu.innerHTML = menuHtml(layout, byKey, traits, visibleCols().length);
  }

  // Focus `el`, or (disabled, e.g. an arrow at the end) its row's next
  // enabled control.
  function focusMenuControl(el) {
    const target = el && !el.disabled ? el : el?.closest("li").querySelector(ENABLED_BTN);
    if (target) target.focus();
    else focusFirstItem(menu);
  }

  // Opened and placed synchronously (no unpositioned first paint). Light
  // dismiss closes the menu on pointerup, before this click: pointerdown
  // records whether it was open so a second click closes, not reopens.
  // The click bubbles on (col-sort skips th controls), so document-level
  // listeners such as other menus' close-on-click still see it.
  function wireMenuButton(btn) {
    let wasOpen = false;
    btn.addEventListener("pointerdown", () => { wasOpen = isMenuOpen(); });
    btn.addEventListener("click", () => {
      const close = wasOpen || isMenuOpen();
      wasOpen = false;
      if (close) {
        closeMenu();
        return;
      }
      renderMenu();
      menu.showPopover();
      positionMenu(menu, btn);
      focusFirstItem(menu);
    });
  }

  function isMenuOpen() {
    return menu.matches(":popover-open");
  }

  // The beforetoggle handler below returns focus to the button.
  function closeMenu() {
    if (isMenuOpen()) menu.hidePopover();
  }

  function onMenuClick(ev) {
    const btn = ev.target.closest(MENU_CONTROL_SEL);
    if (!btn || btn.disabled) return;
    const key = btn.closest(`.${MENU_COL_CLASS}`)?.dataset.key;
    const action = btn.dataset.action;
    if (btn.dataset.dir) moveColumn(layout, key, Number(btn.dataset.dir));
    else if (action === MENU_ACTION.TOGGLE) toggleColumn(layout, key, visibleCols().length);
    else if (action === MENU_ACTION.FIT) fitColumns();
    else if (action === MENU_ACTION.RESET) Object.assign(layout, reconcileLayout(columns, {}, !traits.reorder));
    else return;
    saveLayout();
    relayout();
    // Fit and Reset are one-shot table commands: close so the result shows.
    if (action === MENU_ACTION.FIT || action === MENU_ACTION.RESET) {
      closeMenu();
      return;
    }
    renderMenu();
    // A key press refocuses the same control, so repeats keep moving the
    // column; a mouse click (detail > 0), what the rows moved under the still
    // pointer, as hovering would. Focus picks the row showing arrows (CSS).
    const under = ev.detail > 0
      && document.elementFromPoint(ev.clientX, ev.clientY)?.closest(MENU_CONTROL_SEL);
    focusMenuControl(under && menu.contains(under)
      ? under : menu.querySelector(`[${FID_ATTR}="${CSS.escape(btn.dataset.fid)}"]`));
  }

  if (hasMenu) {
    // Top-layer popover: escapes the scrollers' and WinBox's overflow clip.
    menu = document.createElement("ul");
    menu.className = "tmb-dropdown dt-menu";
    menu.setAttribute("popover", "auto");
    menu.addEventListener("click", onMenuClick);
    attachMenuNav(menu, { close: closeMenu });
    // Closing with focus inside (Esc, Tab) would drop it to <body>: hand it
    // back to the button instead. (A light-dismiss click already moved focus.)
    menu.addEventListener("beforetoggle", (ev) => {
      if (ev.newState === "closed" && menu.contains(document.activeElement)) {
        thead.querySelector(`.${MENU_BTN_CLASS}`)?.focus();
      }
    });
    wrap.append(menu);
  }

  relayout();

  return {
    wrap,
    headTable,
    bodyTable,
    tbody,
    // rowNav's current row element, or null.
    currentRow: () => tbody.querySelector(CURRENT_ROW_SEL),
    // Re-apply each rendered row's class and current mark in place, without
    // rebuilding cells: a cheap selection repaint for long lists. Replaces the
    // whole className, so classes added outside rowAttrs (e.g. multi-select
    // marks) are dropped -- tables with those must re-render instead.
    refreshRows() {
      renderedRows.forEach((r, i) => {
        const a = attrsFor ? attrsFor(r, i) : {};
        const tr = tbody.rows[i];
        tr.className = a.class ?? "";
        if (a[CURRENT_ATTR]) tr.setAttribute(CURRENT_ATTR, "true");
        else tr.removeAttribute(CURRENT_ATTR);
      });
    },
    // Active sort { key, dir } (top of the stack) or null; setSort(state|null)
    // routes through the header's arrow + persist + re-render path.
    getSort: () => sortCtrl?.stack()[0] ?? null,
    setSort: (state) => sortCtrl?.set(state),
    render(nextRows) {
      rows = nextRows;
      renderBody(RENDER_REASON.DATA);
    },
  };
}
