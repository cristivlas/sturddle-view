// Table factory: builds a split head/body table (split-table.js) from a
// column list and wires the requested traits -- sort, resize, reorder, hide,
// fit -- so a table declares its columns instead of re-wiring each helper.
// The header and column layout are column-header.js's; this module renders
// the whole body from rows.
//
// Column descriptor: column-header.js's, plus cell(row) => html, tdClass?,
// tdTitle?(row) => text, and the sort-stack.js fields (field, numeric,
// firstDir, tiebreak, sortable). Widths are % weights.

import { attachLayeredSort, sortByStack } from "./sort-stack.js";
import { attachColumnSort } from "./col-sort.js";
import { createColumnHeader } from "./column-header.js";
import { escapeHtml, LIST_NAV_KEYS, wireArrowKeyNav } from "./wb-utils.js";

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

// Why the body was rebuilt (createTable's onRender argument).
export const RENDER_REASON = Object.freeze({ DATA: "data", SORT: "sort", LAYOUT: "layout" });

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
  let rows = null;
  // Rows in rendered order (tbody.rows[i] <-> renderedRows[i]).
  let renderedRows = [];
  let sortCtrl = null;
  // The shown columns, in order (set on every header rebuild).
  let shown = [];
  // Cursor-mode rowNav's current row, by data-id (survives re-renders).
  const cursor = { id: null };
  const attrsFor = traits.rowNav ? rowNavAttrs(rowAttrs, traits.rowNav, cursor) : rowAttrs;

  const header = createColumnHeader({
    columns, layoutKey, minPct,
    traits,
    classes: {
      wrap: `${cls}-table-wrap`,
      headScroll: `${cls}-head-scroll`,
      headTable: `${tableClass} ${cls}-head-tbl`,
      bodyScroll: `${cls}-body-scroll`,
      bodyTable: `${tableClass} ${cls}-body-tbl`,
    },
    // Sort binds to the rebuilt ths, then the body follows the new columns.
    onLayout(cols) {
      shown = cols;
      if (traits.sort) sortCtrl = attachSort(cols);
      renderBody(RENDER_REASON.LAYOUT);
    },
  });
  const { wrap, headTable, bodyTable, tbody } = header;
  if (traits.rowNav) attachRowNav(bodyTable, traits.rowNav, cursor);

  // Hidden columns drop out of the sort (the arrow follows the next shown
  // entry); they stay in the persisted stack, so showing one restores it.
  function renderBody(reason) {
    if (!rows) return;
    const cols = shown;
    const sorted = rows.slice();
    if (sortCtrl) {
      const shownKeys = new Set(cols.map((c) => c.key));
      sortByStack(sorted, sortCtrl.stack().filter((st) => shownKeys.has(st.key)), columns, traits.sort.group);
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

  header.relayout();

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
