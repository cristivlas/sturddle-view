// Engine list + CRUD logic for the Settings dialog Engines tab.
// Horizontal ribbon at the top (Add / Use / Settings / Search / Sort /
// Remove), an inline drop-up search bar at the bottom, and a list whose
// height is sized to fit the dialog body via JS measurement.

import { attachColumnResize } from "./col-resize.js";
import { apiErrorDetail, confirm, pickFile, reportError, toast } from "./dialogs.js";
import { showEngineOptionsDialog } from "./engine-options-dialog.js";

const COL_PCTS_KEY = "sturddle:engines:colPcts3";
const DEFAULT_PCTS = [20, 12, 68];
const SORT_KEY_LS = "sturddle:engines:sortOrder";
// Height of the overlaid search bar; matches the CSS rule. Added as
// bottom padding on the list while open so the last row stays visible
// above the bar.
const INLINE_SEARCH_RESERVED_PX = 48;

export function mountEngineList(container, api, opts = {}) {
  const { colPctsKey = COL_PCTS_KEY } = opts;

  container.innerHTML = `
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

  const detailUseBtn = container.querySelector(".engines-detail-use");
  const detailRemoveBtn = container.querySelector(".engines-detail-remove");
  const detailOptionsBtn = container.querySelector(".engines-detail-options");
  const addBtn = container.querySelector(".engines-add");
  const list = container.querySelector(".engines-list");
  const emptyEl = container.querySelector(".engines-empty");
  const emptyMsg = emptyEl.querySelector(".empty-message");

  let engines = [];
  const inflightEngineChecks = new Map();

  function getEngineFresh(id) {
    if (inflightEngineChecks.has(id)) return inflightEngineChecks.get(id);
    const p = api("GET", `/engines/${id}`).finally(() => inflightEngineChecks.delete(id));
    inflightEngineChecks.set(id, p);
    return p;
  }

  // TODO: multi-select for bulk Remove (large engine libraries from
  // tester users). See docs/spec.md "Open / Deferred".
  let selectedDetailId = null;
  let activeId = null;
  let filterText = "";
  let sortOrder = ["asc", "desc", "none"].includes(localStorage.getItem(SORT_KEY_LS))
    ? localStorage.getItem(SORT_KEY_LS) : "none";

  // Last (count, activeId) pair we broadcast on sturddle:engines-changed.
  // Tracks across refreshes so the initial mount doesn't fire spuriously
  // if state matches what listeners (e.g. Play) already fetched.
  let lastBroadcast = { count: -1, activeId: undefined };

  async function refresh() {
    try {
      const body = await api("GET", "/engines");
      engines = body.engines;
      activeId = body.selected_id;
      if (selectedDetailId && !engines.some((e) => e.id === selectedDetailId)) {
        selectedDetailId = null;
      }
      if (selectedDetailId === null) {
        selectedDetailId = activeId || (engines[0]?.id ?? null);
      }
      renderAll();
      if (engines.length !== lastBroadcast.count || activeId !== lastBroadcast.activeId) {
        lastBroadcast = { count: engines.length, activeId };
        window.dispatchEvent(new CustomEvent("sturddle:engines-changed", {
          detail: { count: engines.length, activeId },
        }));
      }
    } catch (e) {
      reportError(null, "engines", e);
    }
  }

  function renderAll() {
    renderList();
    syncDetailButtons();
  }

  function renderList() {
    list.innerHTML = "";
    const needle = filterText.trim().toLowerCase();
    let visible = needle
      ? engines.filter((e) => e.name.toLowerCase().includes(needle))
      : engines.slice();
    if (sortOrder === "asc" || sortOrder === "desc") {
      const dir = sortOrder === "asc" ? 1 : -1;
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

    if (engines.length === 0) {
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
      if (e.id === selectedDetailId) tr.classList.add("focused");
      if (e.id === activeId) tr.classList.add("active");

      const nameTd = document.createElement("td");
      nameTd.className = "engines-list-name";
      const nameText = document.createElement("span");
      nameText.className = "engines-list-name-text";
      nameText.textContent = e.name;
      nameTd.title = e.name;
      nameTd.appendChild(nameText);

      const activeTd = document.createElement("td");
      activeTd.className = "engines-list-active-cell";
      if (e.id === activeId) {
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
        selectedDetailId = e.id;
        renderAll();
      });
      tr.addEventListener("dblclick", () => {
        selectedDetailId = e.id;
        renderAll();
        openOptionsForSelected();
      });
      list.appendChild(tr);
    }
  }

  function syncDetailButtons() {
    const e = engines.find((x) => x.id === selectedDetailId);
    const has = !!e;
    detailOptionsBtn.disabled = !has;
    detailRemoveBtn.disabled = !has;
    detailUseBtn.disabled = !has || (e && e.id === activeId);
  }

  // Sort.
  const sortAscBtn = container.querySelector(".engines-sort-asc");
  const sortDescBtn = container.querySelector(".engines-sort-desc");
  function syncSortButtons() {
    sortAscBtn.classList.toggle("is-active", sortOrder === "asc");
    sortDescBtn.classList.toggle("is-active", sortOrder === "desc");
  }
  function setSort(next) {
    sortOrder = sortOrder === next ? "none" : next;
    localStorage.setItem(SORT_KEY_LS, sortOrder);
    syncSortButtons();
    renderList();
  }
  sortAscBtn.addEventListener("click", () => setSort("asc"));
  sortDescBtn.addEventListener("click", () => setSort("desc"));
  syncSortButtons();

  // Search. The search-wrap is positioned absolutely over the bottom of
  // the panel and slides up from below, so no surrounding layout changes
  // when it opens/closes.
  {
    const searchBtn = container.querySelector(".engines-search-btn");
    const searchWrap = container.querySelector(".engines-search-wrap");
    const searchInput = container.querySelector(".engines-search");
    const tableWrap = container.querySelector(".engines-table-wrap");

    searchInput.addEventListener("input", () => {
      filterText = searchInput.value || "";
      searchBtn.classList.toggle("is-active", !!filterText);
      renderList();
    });

    function closeSearch() {
      searchWrap.classList.remove("open");
      searchBtn.classList.remove("is-active");
      tableWrap.style.paddingBottom = "";
      searchInput.value = "";
      filterText = "";
      renderList();
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
  }

  async function activateSelected() {
    if (!selectedDetailId) return;
    const e = engines.find((x) => x.id === selectedDetailId);
    if (!e || e.id === activeId) return;
    try {
      await api("POST", `/engines/${selectedDetailId}/select`);
      refresh();
    } catch (err) {
      reportError(null, "select", err);
    }
  }
  detailUseBtn.addEventListener("click", activateSelected);

  list.addEventListener("keydown", (ev) => {
    if (ev.key !== " ") return;
    ev.preventDefault();
    activateSelected();
  });

  detailRemoveBtn.addEventListener("click", async () => {
    if (!selectedDetailId) return;
    let e;
    try { e = await getEngineFresh(selectedDetailId); } catch (err) { reportError(null, "remove", err); return; }
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
      await api("DELETE", `/engines/${selectedDetailId}`);
      toast(`Removed ${e.name}`, { variant: "success" });
      selectedDetailId = null;
      refresh();
    } catch (err) {
      reportError(null, "remove", err);
    }
  });

  async function openOptionsForSelected() {
    if (!selectedDetailId) return;
    let engine;
    try { engine = await getEngineFresh(selectedDetailId); } catch (err) { reportError(null, "edit", err); return; }
    const locked = engine.locked?.length ? engine.locked.map((t) => `${t.name} (${t.status})`).join(", ") : null;
    if (locked) { toast(`Cannot edit ${engine.name}: in use by ${locked}`, { variant: "warning" }); return; }
    // Auto re-probe on first open if UCI options are empty -- heals transient
    // spawn errors from add-time so the user doesn't see an empty dialog.
    let probeError = null;
    if (engine && !Object.keys(engine.option_schema || {}).length) {
      try {
        engine = await api("POST", `/engines/${engine.id}/refresh-schema`, {});
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
        await api("DELETE", `/engines/${engine.id}`);
        toast(`Removed ${engine.name}`, { variant: "success" });
        selectedDetailId = null;
        refresh();
      } catch (err) {
        reportError(null, "remove", err);
      }
      return;
    }
    while (engine) {
      const result = await showEngineOptionsDialog({ engine, api, probeError });
      if (result === null) break;
      if (result?.__refresh || result?.__reopen) {
        engine = result.engine;
        // Re-open carries forward the probe outcome: __refresh sets
        // result.probeError when the in-dialog probe failed; __reopen
        // (post-save) has no probe and should clear any prior note.
        probeError = result.probeError ?? null;
        continue;
      }
      refresh();
      break;
    }
  }
  detailOptionsBtn.addEventListener("click", openOptionsForSelected);

  addBtn.addEventListener("click", async () => {
    const path = await pickFile({
      api,
      title: "Add engine",
      mode: "executable",
    });
    if (!path) return;
    try {
      const created = await api("POST", "/engines", { path });
      toast(`Added ${created.name}`, { variant: "success" });
      selectedDetailId = created.id;
      refresh();
    } catch (e) {
      reportError(null, "add", e);
    }
  });

  // Column resize.
  {
    const colEls = Array.from(container.querySelectorAll(".engines-table col"));
    const wrapEl = container.querySelector(".engines-table-wrap");
    const tableEl = container.querySelector(".engines-table");
    const grips = Array.from(container.querySelectorAll(".engines-table .th-grip"));
    const minPct = 8;
    const colPcts = DEFAULT_PCTS.slice();

    attachColumnResize({
      table: tableEl,
      grips,
      overlayHost: wrapEl,
      storageKey: colPctsKey,
      sizes: colPcts,
      unit: "pct",
      applySizes(sizes, ctx) {
        if (ctx) {
          const { deltaFrac, startSizes, gripIdx } = ctx;
          const dPct = deltaFrac * 100;
          let a = startSizes[gripIdx] + dPct;
          let b = startSizes[gripIdx + 1] - dPct;
          if (a < minPct) { b -= minPct - a; a = minPct; }
          if (b < minPct) { a -= minPct - b; b = minPct; }
          sizes[gripIdx] = a;
          sizes[gripIdx + 1] = b;
        }
        colEls.forEach((c, i) => { c.style.width = sizes[i] + "%"; });
      },
    });
  }

  // Size the table-wrap explicitly so the LIST scrolls internally instead
  // of the dialog body. The search bar is positioned absolutely over the
  // bottom of the panel and does not occupy flow space, so the list
  // height only needs to account for the ribbon.
  const wrapEl = container.querySelector(".engines-table-wrap");
  const bodyMainEl = container.querySelector(".engines-body-main");
  const ribbonEl = container.querySelector(".engines-ribbon");
  function sizeWrap() {
    const dialog = container.closest("wa-dialog");
    const body = dialog?.shadowRoot?.querySelector('[part~="body"]');
    const topAnchor = bodyMainEl.getBoundingClientRect().top;
    const bodyBottom = body
      ? body.getBoundingClientRect().bottom
      : window.innerHeight - 8;
    const ribbonH = ribbonEl?.offsetHeight || 0;
    let h = Math.max(120, Math.floor(bodyBottom - topAnchor - ribbonH - 8));
    wrapEl.style.height = h + "px";
    // Expose ribbon height so the absolute search-wrap can anchor above
    // the ribbon.
    container.style.setProperty("--engines-ribbon-h", ribbonH + "px");
    if (body) {
      const overflow = body.scrollHeight - body.clientHeight;
      if (overflow > 0) {
        h = Math.max(120, h - overflow);
        wrapEl.style.height = h + "px";
      }
    }
  }
  sizeWrap();
  requestAnimationFrame(sizeWrap);

  // Coalesce rapid resize bursts into one measurement per frame.
  let resizeRaf = 0;
  function scheduleSizeWrap() {
    if (resizeRaf) return;
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      sizeWrap();
    });
  }

  const dialog = container.closest("wa-dialog");
  const tabGroup = container.closest("wa-tab-group");
  const ro = (typeof ResizeObserver !== "undefined" && dialog)
    ? new ResizeObserver(scheduleSizeWrap) : null;
  if (ro) {
    ro.observe(dialog);
    // Observe bodyMain so future panel content changes (added rows,
    // font-size shifts) trigger a re-measure automatically. sizeWrap
    // only mutates a child (engines-table-wrap), so no feedback loop.
    ro.observe(bodyMainEl);
  }

  // Re-measure when switching back to the engines tab: other tabs are
  // content-sized so the body's scrollHeight may have changed.
  const onTabShow = (ev) => {
    if (ev.detail?.name === container.closest("wa-tab-panel")?.name) {
      scheduleSizeWrap();
    }
  };
  if (tabGroup) tabGroup.addEventListener("wa-tab-show", onTabShow);

  window.addEventListener("resize", scheduleSizeWrap);

  // Cleanup on dialog close so listeners and observers don't leak across
  // repeated open/close cycles.
  if (dialog) {
    dialog.addEventListener("wa-after-hide", function cleanup(ev) {
      if (ev.target !== dialog) return;
      if (resizeRaf) cancelAnimationFrame(resizeRaf);
      window.removeEventListener("resize", scheduleSizeWrap);
      if (tabGroup) tabGroup.removeEventListener("wa-tab-show", onTabShow);
      if (ro) ro.disconnect();
      dialog.removeEventListener("wa-after-hide", cleanup);
    });
  }

  refresh();
  return { refresh };
}
