// Engine list + CRUD logic for the Settings dialog Engines tab.
// Horizontal ribbon at the top (Add / Use / Settings / Search / Sort /
// Remove), an inline drop-up search bar at the bottom, and a list whose
// height is sized to fit the dialog body via JS measurement.
//
// The controller closes over a single `ctx` object (engines, selectedDetailId,
// activeId, filterText, sort, dom refs, api) so render/CRUD/search/sort
// can live as module-level functions instead of one giant closure. The table
// itself (resize, header sort) is a data-table.js instance.

import { APP_EVT } from "./app-events.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { apiErrorDetail, confirm, pickFile, reportError, toast } from "./dialogs.js";
import { escapeHtml, guard, markSelectable, suppressMultiClickSelect } from "./wb-utils.js";
import { showEngineOptionsDialog } from "./engine-options-dialog.js";
import { createWrapSizer } from "./list-wrap-sizer.js";
import { attachButtonSort, scrollSortedRowIntoView, SORT_ASC_LABEL, SORT_DESC_LABEL, SORT_DIR } from "./col-sort.js";
import { saveRaw } from "./storage.js";
import { createTable, RENDER_REASON } from "./data-table.js";
import { wireInlineSearch } from "./inline-search.js";

// Engine listing without the lazy probes, for name/selection lookups.
export const ENGINES_NO_PROBE_PATH = "/engines?probe=false";

const COL_NAME = "name";
const COL_ACTIVE = "active";
const COL_PATH = "path";
const ENGINE_ROW_CLASS = "engines-list-item";
const ENGINE_ROW_SEL = `tr.${ENGINE_ROW_CLASS}`;
const SELECTED_CLASS = "focused";
const SELECTED_ROW_SEL = `tr.${SELECTED_CLASS}`;
const ACTIVE_CLASS = "active";
const EMPTY_CELL_CLASS = "engines-list-empty muted";
const NO_MATCH_MSG = "No engines match.";
const MIN_COL_PCT = 8;
const ENGINES_RIBBON_H_VAR = "--engines-ribbon-h";

const ENGINES_LIST_HTML = `
    <div class="engines-list-host">
      <div class="engines-body-main">
        <div class="engines-body-content">
          <div class="engines-empty hidden">
            <p class="empty-message"></p>
          </div>
        </div>
      </div>

      <div class="engines-ribbon" role="toolbar" aria-label="Engine actions">
        <button class="ribbon-btn engines-add" aria-label="Add engine" title="Add engine">
          <wa-icon name="plus"></wa-icon>
        </button>
        <button class="ribbon-btn engines-detail-use" disabled aria-label="Use as active engine" title="Use as active engine">
          <wa-icon name="check"></wa-icon>
        </button>
        <button class="ribbon-btn engines-detail-options" disabled aria-label="Engine settings" title="Engine settings">
          <wa-icon name="sliders"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn engines-search-btn" aria-label="Search engines" title="Search engines">
          <wa-icon name="magnifying-glass"></wa-icon>
        </button>
        <button class="ribbon-btn engines-sort-asc" aria-label="${SORT_ASC_LABEL}" title="${SORT_ASC_LABEL}">
          <wa-icon name="arrow-down-a-z"></wa-icon>
        </button>
        <button class="ribbon-btn engines-sort-desc" aria-label="${SORT_DESC_LABEL}" title="${SORT_DESC_LABEL}">
          <wa-icon name="arrow-down-z-a"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn ribbon-btn--danger engines-detail-remove" disabled aria-label="Remove engine" title="Remove engine">
          <wa-icon name="trash"></wa-icon>
        </button>
      </div>

      <div class="engines-search-wrap inline-search">
        <wa-input class="engines-search" size="small" placeholder="Search engines..." clearable autocomplete="off"></wa-input>
      </div>
    </div>
  `;

function getEngineFresh(ctx, id) {
  if (ctx.inflightEngineChecks.has(id)) return ctx.inflightEngineChecks.get(id);
  const p = ctx.api("GET", `/engines/${id}`).finally(() => ctx.inflightEngineChecks.delete(id));
  ctx.inflightEngineChecks.set(id, p);
  return p;
}

async function refresh(ctx) {
  try {
    const body = await ctx.api("GET", "/engines");
    ctx.engines = body.engines;
    ctx.activeId = body.selected_id;
    if (ctx.selectedDetailId && !ctx.engines.some((e) => e.id === ctx.selectedDetailId)) {
      ctx.selectedDetailId = null;
    }
    if (ctx.selectedDetailId === null) {
      ctx.selectedDetailId = ctx.activeId || (ctx.engines[0]?.id ?? null);
    }
    renderAll(ctx);
    if (ctx.engines.length !== ctx.lastBroadcast.count || ctx.activeId !== ctx.lastBroadcast.activeId) {
      ctx.lastBroadcast = { count: ctx.engines.length, activeId: ctx.activeId };
      window.dispatchEvent(new CustomEvent(APP_EVT.ENGINES_CHANGED, {
        detail: { count: ctx.engines.length, activeId: ctx.activeId },
      }));
    }
  } catch (e) {
    reportError(null, "engines", e);
  }
}

function renderAll(ctx) {
  renderList(ctx);
  syncDetailButtons(ctx);
}

// Rows: { id, name, path, active } (active: 1 for the active engine, so the
// numeric Active column orders it first/last). Name is the fixed final
// tiebreaker so order is deterministic.
const ENGINE_COLUMNS = [
  { key: COL_NAME, label: "Name", width: 24, firstDir: SORT_DIR.ASC, tiebreak: true,
    tdClass: "engines-list-name", tdTitle: (r) => r.name,
    cell: (r) => `<span class="engines-list-name-text">${escapeHtml(r.name)}</span>` },
  { key: COL_ACTIVE, label: "Active", width: 16, numeric: true, firstDir: SORT_DIR.DESC,
    tdClass: "engines-list-active-cell",
    cell: (r) => (r.active ? `<wa-icon name="check" class="engines-list-active-badge"></wa-icon>` : "") },
  { key: COL_PATH, label: "Path", width: 60, firstDir: SORT_DIR.ASC,
    tdClass: "engines-list-path", tdTitle: (r) => r.path, cell: (r) => escapeHtml(r.path) },
];

function renderList(ctx) {
  const { emptyEl, emptyMsg, addBtn } = ctx;
  if (ctx.engines.length === 0) {
    ctx.table.render([]);
    emptyEl.classList.remove("hidden");
    emptyMsg.replaceChildren();
    const addLink = document.createElement("button");
    addLink.type = "button";
    addLink.className = "toast-icon-btn";
    addLink.setAttribute("aria-label", "Add engine");
    addLink.setAttribute("title", "Add engine");
    const addIc = document.createElement("wa-icon");
    addIc.setAttribute("name", "plus");
    addLink.appendChild(addIc);
    addLink.addEventListener("click", () => addBtn.click());
    emptyMsg.append("No engines yet -- click ", addLink, " to add one.");
    return;
  }
  emptyEl.classList.add("hidden");
  const needle = ctx.filterText.trim().toLowerCase();
  const visible = needle ? ctx.engines.filter((e) => e.name.toLowerCase().includes(needle)) : ctx.engines;
  ctx.table.render(visible.length
    ? visible.map((e) => ({
      id: e.id, name: e.name, path: e.path || "",
      active: e.id === ctx.activeId ? 1 : 0, selected: e.id === ctx.selectedDetailId,
    }))
    : [{ emptyText: NO_MATCH_MSG }]);
}

// Click on an engine row: select it. Returns whether a row was hit.
function selectEngineRow(ctx, ev) {
  const id = ev.target.closest(ENGINE_ROW_SEL)?.dataset.id;
  if (!id) return false;
  ctx.selectedDetailId = id;
  renderAll(ctx);
  return true;
}

// Build the list's table (data-table.js: resize + single-column sort, no
// columns menu) into the body content, with the empty note inside its wrap.
function buildEngineTable(container, layoutKey, onRender) {
  const table = createTable({
    cls: "engines",
    tableClass: "engines-table",
    columns: ENGINE_COLUMNS,
    layoutKey,
    minPct: MIN_COL_PCT,
    traits: {
      sort: { single: true, sortKey: STORAGE_KEY.ENGINES_HEADER_SORT },
      resize: true,
      // Arrows move the selection; a row click selects (delegated in mount).
      rowNav: { isSelected: (r) => r.selected, onSelect: (row) => row.click() },
    },
    rowAttrs: (r) => (r.emptyText != null ? {} : {
      class: [ENGINE_ROW_CLASS, r.selected && SELECTED_CLASS, r.active && ACTIVE_CLASS].filter(Boolean).join(" "),
      "data-id": r.id,
    }),
    fullRow: (r) => (r.emptyText != null ? { html: escapeHtml(r.emptyText), tdClass: EMPTY_CELL_CLASS } : null),
    onRender,
  });
  markSelectable(table.headTable);
  markSelectable(table.bodyTable);
  table.wrap.prepend(container.querySelector(".engines-empty"));
  container.querySelector(".engines-body-content").append(table.wrap);
  return table;
}

function syncDetailButtons(ctx) {
  const e = ctx.engines.find((x) => x.id === ctx.selectedDetailId);
  const has = !!e;
  ctx.detailOptionsBtn.disabled = !has;
  ctx.detailRemoveBtn.disabled = !has;
  ctx.detailUseBtn.disabled = !has || (e && e.id === ctx.activeId);
}

async function activateSelected(ctx) {
  if (!ctx.selectedDetailId) return;
  const e = ctx.engines.find((x) => x.id === ctx.selectedDetailId);
  if (!e || e.id === ctx.activeId) return;
  try {
    await ctx.api("POST", `/engines/${ctx.selectedDetailId}/select`);
    refresh(ctx);
  } catch (err) {
    reportError(null, "select", err);
  }
}

async function removeSelected(ctx) {
  if (!ctx.selectedDetailId) return;
  let e;
  try { e = await getEngineFresh(ctx, ctx.selectedDetailId); } catch (err) { reportError(null, "remove", err); return; }
  const locked = e.locked?.length ? e.locked.map((t) => `${t.name} (${t.status})`).join(", ") : null;
  if (locked) { toast(`Cannot remove ${e.name}: in use by ${locked}`, { variant: "warning" }); return; }
  const ok = await confirm({
    title: "Remove engine",
    message: `Remove ${e.name}?`,
    okLabel: "Remove",
    destructive: true,
  });
  if (!ok) return;
  try {
    await ctx.api("DELETE", `/engines/${ctx.selectedDetailId}`);
    toast(`Removed ${e.name}`, { variant: "success" });
    ctx.selectedDetailId = null;
    refresh(ctx);
  } catch (err) {
    reportError(null, "remove", err);
  }
}

async function openOptionsForSelected(ctx) {
  if (!ctx.selectedDetailId) return;
  let engine;
  try { engine = await getEngineFresh(ctx, ctx.selectedDetailId); } catch (err) { reportError(null, "edit", err); return; }
  const locked = engine.locked?.length ? engine.locked.map((t) => `${t.name} (${t.status})`).join(", ") : null;
  if (locked) { toast(`Cannot edit ${engine.name}: in use by ${locked}`, { variant: "warning" }); return; }
  // Auto re-probe on first open if UCI options are empty (heals transient
  // add-time spawn errors) or uci_name was never captured (entries that
  // predate uci_name persistence -- backfills it for Reset).
  let probeError = null;
  const hadSchema = !!Object.keys(engine.option_schema || {}).length;
  if (!hadSchema || engine.uci_name == null) {
    try {
      engine = await ctx.api("POST", `/engines/${engine.id}/refresh-schema`, {});
    } catch (e) {
      probeError = apiErrorDetail(e);
    }
  }
  // Probe failing with no cached schema -> the Options form has nothing
  // useful to show (broken executable, moved binary, etc). Offer to remove
  // the entry rather than open a torn-up dialog the user can't edit. With
  // a cached schema the dialog still opens, showing the probe-error banner.
  if (probeError && !hadSchema) {
    const ok = await confirm({
      message: `Engine "${engine.name}" cannot be launched: ${probeError} Remove it from your engines?`,
      okLabel: "Remove",
      destructive: true,
    });
    if (!ok) return;
    try {
      await ctx.api("DELETE", `/engines/${engine.id}`);
      toast(`Removed ${engine.name}`, { variant: "success" });
      ctx.selectedDetailId = null;
      refresh(ctx);
    } catch (err) {
      reportError(null, "remove", err);
    }
    return;
  }
  // Other engines' names (lowercased) so Reset can dedup the restored
  // UCI name client-side; the server PATCH stays the authoritative check.
  const takenNames = new Set(
    ctx.engines.filter((x) => x.id !== engine.id).map((x) => x.name.toLowerCase()),
  );
  let presetName = null;
  let presetDerived = false;
  while (engine) {
    const result = await showEngineOptionsDialog({
      engine, api: ctx.api, probeError, presetName, presetDerived, takenNames,
    });
    if (result === null) break;
    if (result?.__refresh || result?.__reopen) {
      engine = result.engine;
      // Reopens (Refresh probe / Reset) carry forward the probe outcome
      // and the in-flight name edit.
      probeError = result.probeError ?? null;
      presetName = result.presetName ?? null;
      presetDerived = result.presetDerived ?? false;
      continue;
    }
    refresh(ctx);
    break;
  }
}

async function addEngine(ctx) {
  const path = await pickFile({
    api: ctx.api,
    title: "Add engine",
    mode: "executable",
  });
  if (!path) return;
  try {
    const created = await ctx.api("POST", "/engines", { path });
    toast(`Added ${created.name}`, { variant: "success" });
    ctx.selectedDetailId = created.id;
    refresh(ctx);
  } catch (e) {
    reportError(null, "add", e);
  }
}

// Inline drop-up search (inline-search.js) at the panel bottom, by its
// ribbon. Clicks on the list don't close it, so a filtered row can be picked.
function setupEngineSearch(ctx) {
  const { container } = ctx;
  return wireInlineSearch({
    btn: container.querySelector(".engines-search-btn"),
    bar: container.querySelector(".engines-search-wrap"),
    input: container.querySelector(".engines-search"),
    inside: [container.querySelector(".engines-table-wrap")],
    onInput: (text) => {
      ctx.filterText = text;
      renderList(ctx);
    },
    onClose: () => {
      ctx.filterText = "";
      renderList(ctx);
      // The full list is back: keep the picked row in view (matches openings).
      scrollSortedRowIntoView(container.querySelector(".engines-body-scroll"), SELECTED_ROW_SEL);
    },
  });
}

export function mountEngineList(container, api, opts = {}) {
  const { layoutKey = STORAGE_KEY.ENGINES_LAYOUT } = opts;
  container.innerHTML = ENGINES_LIST_HTML;
  saveRaw(STORAGE_KEY.ENGINES_SORT_ORDER_LEGACY, null);  // clear orphaned pre-header-sort key
  const table = buildEngineTable(container, layoutKey, (reason) => {
    if (reason === RENDER_REASON.SORT) onSorted();
  });

  const ctx = {
    container,
    api,
    engines: [],
    inflightEngineChecks: new Map(),
    // TODO: multi-select for bulk Remove (large engine libraries from
    // tester users).
    selectedDetailId: null,
    activeId: null,
    filterText: "",
    // Last (count, activeId) pair broadcast on sturddle:engines-changed.
    // Tracks across refreshes so the initial mount doesn't fire spuriously
    // if state matches what listeners (e.g. Play) already fetched.
    lastBroadcast: { count: -1, activeId: undefined },
    detailUseBtn: container.querySelector(".engines-detail-use"),
    detailRemoveBtn: container.querySelector(".engines-detail-remove"),
    detailOptionsBtn: container.querySelector(".engines-detail-options"),
    addBtn: container.querySelector(".engines-add"),
    table,
    list: table.tbody,
    emptyEl: container.querySelector(".engines-empty"),
    emptyMsg: container.querySelector(".engines-empty .empty-message"),
    sortAscBtn: container.querySelector(".engines-sort-asc"),
    sortDescBtn: container.querySelector(".engines-sort-desc"),
  };
  // Every action awaits (API / confirm / picker) before acting; guard each
  // once so ribbon clicks, row dblclick, and Space share one in-flight gate.
  ctx.activateGuarded = guard(() => activateSelected(ctx));
  ctx.removeGuarded = guard(() => removeSelected(ctx));
  ctx.openOptionsGuarded = guard(() => openOptionsForSelected(ctx));
  ctx.addGuarded = guard(() => addEngine(ctx));

  const search = setupEngineSearch(ctx);

  ctx.detailUseBtn.addEventListener("click", ctx.activateGuarded);
  // Rows are rebuilt on every render: delegate their clicks.
  ctx.list.addEventListener("click", (ev) => selectEngineRow(ctx, ev));
  ctx.list.addEventListener("dblclick", (ev) => {
    if (selectEngineRow(ctx, ev)) ctx.openOptionsGuarded();
  });
  suppressMultiClickSelect(ctx.list);
  // Space on the focused list (its body table, the Tab stop) activates.
  table.bodyTable.addEventListener("keydown", (ev) => {
    if (ev.key !== " ") return;
    ev.preventDefault();
    ctx.activateGuarded();
  });
  ctx.detailRemoveBtn.addEventListener("click", ctx.removeGuarded);
  ctx.detailOptionsBtn.addEventListener("click", ctx.openOptionsGuarded);
  ctx.addBtn.addEventListener("click", ctx.addGuarded);

  // One sort state, two UIs: the header arrows and the ribbon Name asc/desc
  // buttons (attachButtonSort) both drive/read the table's single sort.
  const nameBtnSort = attachButtonSort({
    ascBtn: ctx.sortAscBtn,
    descBtn: ctx.sortDescBtn,
    key: COL_NAME,
    get: table.getSort,
    set: table.setSort,
  });
  nameBtnSort.sync();
  // Sorting only reorders rows: keep the picked one in view.
  function onSorted() {
    nameBtnSort.sync();
    scrollSortedRowIntoView(ctx.list, SELECTED_ROW_SEL);
  }

  const sizer = createWrapSizer({
    wrapEl: container.querySelector(".engines-table-wrap"),
    anchorEl: container.querySelector(".engines-body-main"),
    ribbonEl: container.querySelector(".engines-ribbon"),
    // The absolute search bar anchors above the ribbon.
    onRibbonHeight: (h) => container.style.setProperty(ENGINES_RIBBON_H_VAR, h + "px"),
  });
  sizer.sizeWrap();
  requestAnimationFrame(sizer.sizeWrap);

  // Cleanup on dialog close so listeners and observers don't leak across
  // repeated open/close cycles.
  const dialog = container.closest("wa-dialog");
  if (dialog) {
    dialog.addEventListener("wa-after-hide", function cleanup(ev) {
      if (ev.target !== dialog) return;
      sizer.teardown();
      search.stopListening();
      dialog.removeEventListener("wa-after-hide", cleanup);
    });
  }

  refresh(ctx);
  return { refresh: () => refresh(ctx) };
}
