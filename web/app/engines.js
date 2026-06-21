// Engine list + CRUD logic for the Settings dialog Engines tab.
// Horizontal ribbon at the top (Add / Use / Settings / Search / Sort /
// Remove), an inline drop-up search bar at the bottom, and a list whose
// height is sized to fit the dialog body via JS measurement.
//
// The controller closes over a single `ctx` object (engines, selectedDetailId,
// activeId, filterText, sortOrder, dom refs, api) so render/CRUD/search/sort
// can live as module-level functions instead of one giant closure. Pure-DOM
// layout (column resize, wrap sizing) lives in engines-list-layout.js.

import { APP_EVT } from "./app-events.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw, saveRaw } from "./storage.js";
import { apiErrorDetail, confirm, pickFile, reportError, toast } from "./dialogs.js";
import { markSelectable } from "./wb-utils.js";
import { showEngineOptionsDialog } from "./engine-options-dialog.js";
import { attachEngineColResize, createWrapSizer } from "./engines-list-layout.js";

const COL_PCTS_KEY = STORAGE_KEY.ENGINES_COL_PCTS;
const SORT_KEY_LS = STORAGE_KEY.ENGINES_SORT_ORDER;
// Height of the overlaid search bar; matches the CSS rule. Added as
// bottom padding on the list while open so the last row stays visible
// above the bar.
const INLINE_SEARCH_RESERVED_PX = 48;

const ENGINES_LIST_HTML = `
    <div class="engines-list-host">
      <div class="engines-body-main">
        <div class="engines-body-content">
          <div class="engines-table-wrap">
            <div class="engines-empty hidden">
              <p class="empty-message"></p>
            </div>
            <table class="engines-table">
              <colgroup>
                <col class="engines-col-name">
                <col class="engines-col-active">
                <col class="engines-col-path">
              </colgroup>
              <thead>
                <tr>
                  <th>Name<span class="th-grip"></span></th>
                  <th class="engines-col-active-hdr">Active<span class="th-grip"></span></th>
                  <th>Path</th>
                </tr>
              </thead>
              <tbody class="engines-list" role="listbox" tabindex="0"></tbody>
            </table>
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
        <button class="ribbon-btn engines-sort-asc" aria-label="Sort A-Z" title="Sort A-Z">
          <wa-icon name="arrow-down-a-z"></wa-icon>
        </button>
        <button class="ribbon-btn engines-sort-desc" aria-label="Sort Z-A" title="Sort Z-A">
          <wa-icon name="arrow-down-z-a"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn ribbon-btn--danger engines-detail-remove" disabled aria-label="Remove engine" title="Remove engine">
          <wa-icon name="trash"></wa-icon>
        </button>
      </div>

      <div class="engines-search-wrap">
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

function renderList(ctx) {
  const { list, emptyEl, emptyMsg, addBtn } = ctx;
  list.innerHTML = "";
  const needle = ctx.filterText.trim().toLowerCase();
  let visible = needle
    ? ctx.engines.filter((e) => e.name.toLowerCase().includes(needle))
    : ctx.engines.slice();
  if (ctx.sortOrder === "asc" || ctx.sortOrder === "desc") {
    const dir = ctx.sortOrder === "asc" ? 1 : -1;
    visible.sort((a, b) => dir * a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
  }

  function emptyRow(text) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 2;
    td.className = "engines-list-empty muted";
    td.textContent = text;
    tr.appendChild(td);
    list.appendChild(tr);
  }

  if (ctx.engines.length === 0) {
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
  if (visible.length === 0) { emptyRow("No engines match."); return; }

  for (const e of visible) {
    const tr = document.createElement("tr");
    tr.className = "engines-list-item";
    tr.dataset.engineId = e.id;
    if (e.id === ctx.selectedDetailId) tr.classList.add("focused");
    if (e.id === ctx.activeId) tr.classList.add("active");

    const nameTd = document.createElement("td");
    nameTd.className = "engines-list-name";
    const nameText = document.createElement("span");
    nameText.className = "engines-list-name-text";
    nameText.textContent = e.name;
    nameTd.title = e.name;
    nameTd.appendChild(nameText);

    const activeTd = document.createElement("td");
    activeTd.className = "engines-list-active-cell";
    if (e.id === ctx.activeId) {
      const badge = document.createElement("wa-icon");
      badge.name = "check";
      badge.className = "engines-list-active-badge";
      activeTd.appendChild(badge);
    }

    const pathTd = document.createElement("td");
    pathTd.className = "engines-list-path";
    pathTd.textContent = e.path || "";
    if (e.path) pathTd.title = e.path;

    tr.appendChild(nameTd);
    tr.appendChild(activeTd);
    tr.appendChild(pathTd);
    tr.addEventListener("click", () => {
      ctx.selectedDetailId = e.id;
      renderAll(ctx);
    });
    tr.addEventListener("dblclick", () => {
      ctx.selectedDetailId = e.id;
      renderAll(ctx);
      openOptionsForSelected(ctx);
    });
    list.appendChild(tr);
  }
}

function syncDetailButtons(ctx) {
  const e = ctx.engines.find((x) => x.id === ctx.selectedDetailId);
  const has = !!e;
  ctx.detailOptionsBtn.disabled = !has;
  ctx.detailRemoveBtn.disabled = !has;
  ctx.detailUseBtn.disabled = !has || (e && e.id === ctx.activeId);
}

function syncSortButtons(ctx) {
  ctx.sortAscBtn.classList.toggle("is-active", ctx.sortOrder === "asc");
  ctx.sortDescBtn.classList.toggle("is-active", ctx.sortOrder === "desc");
}

function setSort(ctx, next) {
  ctx.sortOrder = ctx.sortOrder === next ? "none" : next;
  saveRaw(SORT_KEY_LS, ctx.sortOrder);
  syncSortButtons(ctx);
  renderList(ctx);
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
  // Auto re-probe on first open if UCI options are empty -- heals transient
  // spawn errors from add-time so the user doesn't see an empty dialog.
  let probeError = null;
  if (engine && !Object.keys(engine.option_schema || {}).length) {
    try {
      engine = await ctx.api("POST", `/engines/${engine.id}/refresh-schema`, {});
    } catch (e) {
      probeError = apiErrorDetail(e);
    }
  }
  // Probe still failing -> the Options form has nothing useful to show
  // (broken executable, moved binary, etc). Offer to remove the entry
  // rather than open a torn-up dialog the user can't meaningfully edit.
  if (probeError) {
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
  while (engine) {
    const result = await showEngineOptionsDialog({ engine, api: ctx.api, probeError });
    if (result === null) break;
    if (result?.__refresh || result?.__reopen) {
      engine = result.engine;
      // Re-open carries forward the probe outcome: __refresh sets
      // result.probeError when the in-dialog probe failed; __reopen
      // (post-save) has no probe and should clear any prior note.
      probeError = result.probeError ?? null;
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

// Inline drop-up search. The search-wrap is positioned absolutely over the
// bottom of the panel and slides up from below, so no surrounding layout
// changes when it opens/closes. Returns a teardown for the document-level
// listeners so a row-commit close (which bypasses closeSearch) can't leak them.
function setupEngineSearch(ctx) {
  const { container } = ctx;
  const searchBtn = container.querySelector(".engines-search-btn");
  const searchWrap = container.querySelector(".engines-search-wrap");
  const searchInput = container.querySelector(".engines-search");
  const tableWrap = container.querySelector(".engines-table-wrap");

  searchInput.addEventListener("input", () => {
    ctx.filterText = searchInput.value || "";
    searchBtn.classList.toggle("is-active", !!ctx.filterText);
    renderList(ctx);
  });

  function closeSearch() {
    searchWrap.classList.remove("open");
    searchBtn.classList.remove("is-active");
    tableWrap.style.paddingBottom = "";
    searchInput.value = "";
    ctx.filterText = "";
    renderList(ctx);
    // Clearing the filter re-renders the full list; keep the picked row
    // in view so the selection doesn't scroll off-screen (matches openings).
    tableWrap.querySelector("tr.focused")?.scrollIntoView({ block: "nearest" });
    document.removeEventListener("pointerdown", onOutsideClick);
    document.removeEventListener("keydown", onSearchKey, true);
  }

  function onOutsideClick(e) {
    // Clicks on filtered list rows must not close search: closing would
    // re-render the unfiltered list before the click resolved and the
    // user would hit a different row than the one they aimed at.
    if (searchWrap.contains(e.target)) return;
    if (searchBtn.contains(e.target)) return;
    if (tableWrap.contains(e.target)) return;
    closeSearch();
  }

  function onSearchKey(e) {
    if (e.key === "Escape") {
      closeSearch();
      e.preventDefault();
      e.stopPropagation();
    }
  }

  searchBtn.addEventListener("click", () => {
    const opening = !searchWrap.classList.contains("open");
    if (opening) {
      searchWrap.classList.add("open");
      searchBtn.classList.add("is-active");
      // Reserve scrollable space inside the list so the bottom row is
      // not covered by the overlaid search bar.
      tableWrap.style.paddingBottom = INLINE_SEARCH_RESERVED_PX + "px";
      searchInput.focus();
      document.addEventListener("pointerdown", onOutsideClick);
      document.addEventListener("keydown", onSearchKey, true);
    } else {
      closeSearch();
    }
  });

  return () => {
    document.removeEventListener("pointerdown", onOutsideClick);
    document.removeEventListener("keydown", onSearchKey, true);
  };
}

export function mountEngineList(container, api, opts = {}) {
  const { colPctsKey = COL_PCTS_KEY } = opts;
  container.innerHTML = ENGINES_LIST_HTML;
  markSelectable(container.querySelector(".engines-table"));

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
    sortOrder: ["asc", "desc", "none"].includes(loadRaw(SORT_KEY_LS))
      ? loadRaw(SORT_KEY_LS) : "none",
    // Last (count, activeId) pair broadcast on sturddle:engines-changed.
    // Tracks across refreshes so the initial mount doesn't fire spuriously
    // if state matches what listeners (e.g. Play) already fetched.
    lastBroadcast: { count: -1, activeId: undefined },
    detailUseBtn: container.querySelector(".engines-detail-use"),
    detailRemoveBtn: container.querySelector(".engines-detail-remove"),
    detailOptionsBtn: container.querySelector(".engines-detail-options"),
    addBtn: container.querySelector(".engines-add"),
    list: container.querySelector(".engines-list"),
    emptyEl: container.querySelector(".engines-empty"),
    emptyMsg: container.querySelector(".engines-empty .empty-message"),
    sortAscBtn: container.querySelector(".engines-sort-asc"),
    sortDescBtn: container.querySelector(".engines-sort-desc"),
  };

  ctx.sortAscBtn.addEventListener("click", () => setSort(ctx, "asc"));
  ctx.sortDescBtn.addEventListener("click", () => setSort(ctx, "desc"));
  syncSortButtons(ctx);

  const teardownSearch = setupEngineSearch(ctx);

  ctx.detailUseBtn.addEventListener("click", () => activateSelected(ctx));
  ctx.list.addEventListener("keydown", (ev) => {
    if (ev.key !== " ") return;
    ev.preventDefault();
    activateSelected(ctx);
  });
  ctx.detailRemoveBtn.addEventListener("click", () => removeSelected(ctx));
  ctx.detailOptionsBtn.addEventListener("click", () => openOptionsForSelected(ctx));
  ctx.addBtn.addEventListener("click", () => addEngine(ctx));

  attachEngineColResize(container, colPctsKey);

  const sizer = createWrapSizer(container);
  sizer.sizeWrap();
  requestAnimationFrame(sizer.sizeWrap);

  // Cleanup on dialog close so listeners and observers don't leak across
  // repeated open/close cycles.
  const dialog = container.closest("wa-dialog");
  if (dialog) {
    dialog.addEventListener("wa-after-hide", function cleanup(ev) {
      if (ev.target !== dialog) return;
      sizer.teardown();
      teardownSearch();
      dialog.removeEventListener("wa-after-hide", cleanup);
    });
  }

  refresh(ctx);
  return { refresh: () => refresh(ctx) };
}
