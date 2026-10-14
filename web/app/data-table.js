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
import { attachColumnSort, TH_LABEL_CLASS } from "./col-sort.js";
import { attachMenuNav, focusFirstItem } from "./menu-nav.js";
import { wireSplitScroll } from "./split-table.js";
import { escapeHtml, LIST_NAV_KEYS, wireArrowKeyNav } from "./wb-utils.js";

const MENU_ICON = "sliders";
const MENU_BTN_LABEL = "Columns";
const FIT_LABEL = "Fit columns";
const RESET_LABEL = "Reset columns";
const UP_ICON = "chevron-up";
const DOWN_ICON = "chevron-down";
const ACTIVE_CLASS = "tmb-active";
const MOVE = Object.freeze({ UP: -1, DOWN: 1 });
const MENU_ACTION = Object.freeze({ TOGGLE: "toggle", FIT: "fit", RESET: "reset" });
const ENABLED_BTN = "button:not(:disabled)";
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
const MENU_GAP_PX = 4;
const MENU_EDGE_PX = 8;
const FULL_PCT = 100;

// Why the body was rebuilt (createTable's onRender argument).
export const RENDER_REASON = Object.freeze({ DATA: "data", SORT: "sort", LAYOUT: "layout" });

// Saved { order, hidden, widths } reconciled with the current column list:
// unknown keys drop, new columns append, alwaysOn columns never hide. An
// empty `saved` yields the defaults. Widths are relative weights.
function reconcileLayout(columns, saved) {
  const byKey = new Map(columns.map((c) => [c.key, c]));
  const order = (Array.isArray(saved.order) ? saved.order : []).filter((k) => byKey.has(k));
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

// Proportional shares of the `natural` widths summing to 100%, each at least
// minPct: columns pinned at the floor drop out and the rest re-share. Too
// many columns for the floor: equal shares.
function fitPcts(natural, minPct) {
  if (minPct * natural.length >= FULL_PCT) return natural.map(() => FULL_PCT / natural.length);
  const out = new Array(natural.length);
  let free = natural.map((_, i) => i);
  let budget = FULL_PCT;
  for (;;) {
    const total = free.reduce((s, i) => s + natural[i], 0) || 1;
    const low = free.filter((i) => (natural[i] / total) * budget < minPct);
    if (!low.length) {
      free.forEach((i) => { out[i] = (natural[i] / total) * budget; });
      return out;
    }
    low.forEach((i) => { out[i] = minPct; });
    budget -= minPct * low.length;
    free = free.filter((i) => !low.includes(i));
  }
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

function toggleColumn(layout, key, visibleCount) {
  if (layout.hidden.has(key)) layout.hidden.delete(key);
  else if (visibleCount > 1) layout.hidden.add(key);
}

function tdHtml(col, row) {
  const cls = col.tdClass ? ` class="${col.tdClass}"` : "";
  const title = col.tdTitle ? ` title="${escapeHtml(col.tdTitle(row))}"` : "";
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
  return `<button class="${cls}" data-fid="${escapeHtml(fid)}"${attrs}${disabled ? " disabled" : ""}>${inner}</button>`;
}

function menuHtml(layout, byKey, traits, visibleCount) {
  const items = layout.order.map((key, i) => {
    const col = byKey.get(key);
    const shown = !layout.hidden.has(key);
    const lockToggle = !traits.hide || col.alwaysOn || (shown && visibleCount === 1);
    const move = (dir, icon) => menuBtnHtml(`${key}:${dir}`, "dt-menu-move", `<wa-icon name="${icon}"></wa-icon>`,
      { disabled: moveTarget(layout, i, dir) < 0, attrs: ` data-dir="${dir}"` });
    const moves = traits.reorder ? move(MOVE.UP, UP_ICON) + move(MOVE.DOWN, DOWN_ICON) : "";
    const toggle = menuBtnHtml(`${key}:${MENU_ACTION.TOGGLE}`,
      `tmb-dd-item dt-menu-toggle${shown ? ` ${ACTIVE_CLASS}` : ""}`, escapeHtml(col.label),
      { disabled: lockToggle, attrs: ` data-action="${MENU_ACTION.TOGGLE}"` });
    return `<li class="dt-menu-col" data-key="${escapeHtml(key)}">${toggle}${moves}</li>`;
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

// traits: { sort?: { sortKey, stackKey?, defaultState?, single? }, resize?,
// reorder?, hide?, fit?, rowNav? }; sort.single keeps one active column (no
// layered stack); rowNav: see attachRowNav. `cls` prefixes the wrap/scroller/table classes; the caller
// appends `wrap` and fills it via render(rows). rowAttrs sets each <tr>'s
// attributes (see trOpen). fullRow(row) => { html, tdClass? } | null renders
// a row as one cell spanning every column (group headings, empty notes).
// onRender(reason) runs after every body rebuild (RENDER_REASON), so callers
// can repaint row state.
export function createTable({
  cls, tableClass, columns, layoutKey, minPct, traits = {}, rowAttrs, fullRow, onRender,
}) {
  const byKey = new Map(columns.map((c) => [c.key, c]));
  const layout = reconcileLayout(columns, loadJson(layoutKey) || {});
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
    if (sortCtrl) sortByStack(sorted, sortCtrl.stack().filter((s) => !layout.hidden.has(s.key)), columns);
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
      ? `<button class="dt-menu-btn" title="${MENU_BTN_LABEL}" aria-label="${MENU_BTN_LABEL}"><wa-icon name="${MENU_ICON}"></wa-icon></button>`
      : "";
    const grip = traits.resize && i < cols.length - 1 ? `<span class="th-grip"></span>` : "";
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
    const menuBtn = thead.querySelector(".dt-menu-btn");
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
        grips: Array.from(thead.querySelectorAll(".th-grip")),
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
    probe.className = `${tableClass} ${cls}-head-tbl ${cls}-body-tbl`;
    Object.assign(probe.style, {
      position: "absolute", visibility: "hidden", left: "0", top: "0",
      tableLayout: "auto", width: "max-content",
    });
    probe.append(thead.cloneNode(true), tbody.cloneNode(true));
    wrap.append(probe);
    const natural = Array.from(probe.rows[0].cells, (c) => c.getBoundingClientRect().width);
    probe.remove();
    storeVisiblePcts(layout, cols, fitPcts(natural, minPct));
  }

  // Rebuilt in place on every change; focus returns to the same control, or
  // (now disabled, e.g. an arrow at the end) its row's next enabled one.
  function renderMenu(focusFid = null) {
    menu.innerHTML = menuHtml(layout, byKey, traits, visibleCols().length);
    if (!focusFid) return;
    const el = menu.querySelector(`[data-fid="${CSS.escape(focusFid)}"]`);
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
    const btn = ev.target.closest("button[data-fid]");
    if (!btn || btn.disabled) return;
    const key = btn.closest(".dt-menu-col")?.dataset.key;
    const action = btn.dataset.action;
    if (btn.dataset.dir) moveColumn(layout, key, Number(btn.dataset.dir));
    else if (action === MENU_ACTION.TOGGLE) toggleColumn(layout, key, visibleCols().length);
    else if (action === MENU_ACTION.FIT) fitColumns();
    else if (action === MENU_ACTION.RESET) Object.assign(layout, reconcileLayout(columns, {}));
    else return;
    saveLayout();
    relayout();
    renderMenu(btn.dataset.fid);
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
        thead.querySelector(".dt-menu-btn")?.focus();
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
    // Active sort { key, dir } (top of the stack) or null; setSort(state|null)
    // routes through the header's arrow + persist + re-render path.
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
    getSort: () => sortCtrl?.stack()[0] ?? null,
    setSort: (state) => sortCtrl?.set(state),
    render(nextRows) {
      rows = nextRows;
      renderBody(RENDER_REASON.DATA);
    },
  };
}
