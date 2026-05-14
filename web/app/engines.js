// Engine list + CRUD logic for the Settings dialog Engines tab.
// Horizontal ribbon at the top (Add / Use / Settings / Search / Sort /
// Remove), an inline drop-up search bar at the bottom, and a list whose
// height is sized to fit the dialog body via JS measurement.

import { apiErrorDetail, confirm, pickFile, reportError, toast } from "./dialogs.js";
import { showEngineOptionsDialog } from "./engine-options-dialog.js";

const COL_PCTS_KEY = "sturddle.engines.colPcts3";
const DEFAULT_PCTS = [20, 12, 68];
const SORT_KEY_LS = "sturddle.engines.sortOrder";
// Reserved height for the inline search bar; matches the CSS .open
// max-height. We pre-shrink/grow the list wrap by this amount before
// toggling the .open class so the body never overflows mid-animation
// (a scrollbar would flash and we'd get jitter).
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

  let selectedDetailId = null;
  let activeId = null;
  let filterText = "";
  let sortOrder = ["asc", "desc", "none"].includes(localStorage.getItem(SORT_KEY_LS))
    ? localStorage.getItem(SORT_KEY_LS) : "none";

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

  // Holds the sizeWrap function reference so the search open/close
  // handlers can trigger a re-measure (the inline search bar changes
  // the height available to the list).
  let sizeWrapRef = null;

  // Search.
  {
    const searchBtn = container.querySelector(".engines-search-btn");
    const searchWrap = container.querySelector(".engines-search-wrap");
    const searchInput = container.querySelector(".engines-search");

    searchInput.addEventListener("input", () => {
      filterText = searchInput.value || "";
      searchBtn.classList.toggle("is-active", !!filterText);
      renderList();
    });

    // Re-run sizeWrap once the search-wrap max-height transition ends
    // OR is canceled (rapid toggles cancel the previous transition
    // without firing transitionend). Filtered on max-height so the
    // sibling padding/opacity transitions don't trigger duplicate work.
    const onMaxHeightSettled = (ev) => {
      if (ev.propertyName !== "max-height") return;
      if (sizeWrapRef) sizeWrapRef();
    };
    searchWrap.addEventListener("transitionend", onMaxHeightSettled);
    searchWrap.addEventListener("transitioncancel", onMaxHeightSettled);

    function closeSearch() {
      searchWrap.classList.remove("open");
      searchBtn.classList.remove("is-active");
      searchInput.value = "";
      filterText = "";
      renderList();
      document.removeEventListener("pointerdown", onOutsideClick);
      document.removeEventListener("keydown", onSearchKey);
      // The wrap will grow back when the search-wrap's max-height
      // transitionend fires (handler above).
    }

    function onOutsideClick(e) {
      if (!searchWrap.contains(e.target) && !searchBtn.contains(e.target)) closeSearch();
    }

    function onSearchKey(e) {
      if (e.key === "Escape") { closeSearch(); e.preventDefault(); }
    }

    searchBtn.addEventListener("click", () => {
      const opening = !searchWrap.classList.contains("open");
      if (opening) {
        // Pre-shrink the wrap by the search bar's reserved height BEFORE
        // adding .open. The body stays under its max-height for the whole
        // open animation, so no scrollbar flash.
        if (sizeWrapRef) {
          const w = container.querySelector(".engines-table-wrap");
          const cur = parseFloat(w.style.height) || w.offsetHeight;
          w.style.height = Math.max(120, cur - INLINE_SEARCH_RESERVED_PX) + "px";
        }
        searchWrap.classList.add("open");
        searchBtn.classList.add("is-active");
        searchInput.focus();
        document.addEventListener("pointerdown", onOutsideClick);
        document.addEventListener("keydown", onSearchKey);
        // sub-pixel correction runs via the transitionend handler above.
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
    while (engine) {
      const result = await showEngineOptionsDialog({ engine, api, probeError });
      if (result === null) break;
      if (result?.__refresh || result?.__reopen) {
        engine = result.engine;
        probeError = null;
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
      if (created.probe_error) {
        toast(
          `Added ${created.name}, but UCI probe failed: ${created.probe_error}`,
          { variant: "warning" },
        );
      } else {
        toast(`Added ${created.name}`, { variant: "success" });
      }
      selectedDetailId = created.id;
      refresh();
    } catch (e) {
      reportError(null, "add", e);
    }
  });

  // Column resize.
  {
    const colEls = Array.from(container.querySelectorAll(".engines-table col"));
    let colPcts = DEFAULT_PCTS.slice();

    function applyColPcts() {
      colEls.forEach((c, i) => { c.style.width = colPcts[i] + "%"; });
    }
    try {
      const saved = JSON.parse(localStorage.getItem(colPctsKey));
      if (Array.isArray(saved) && saved.length === 3) colPcts = saved;
    } catch (e) { /* use defaults */ }
    applyColPcts();

    const wrapEl = container.querySelector(".engines-table-wrap");
    const tableEl = container.querySelector(".engines-table");
    const minPct = 5;

    container.querySelectorAll(".engines-table .th-grip").forEach((grip, gripIdx) => {
      grip.addEventListener("pointerdown", (eDown) => {
        if (eDown.button !== 0) return;
        eDown.preventDefault();
        grip.setPointerCapture(eDown.pointerId);
        grip.classList.add("dragging");
        const startX = eDown.clientX;
        const startA = colPcts[gripIdx], startB = colPcts[gripIdx + 1];
        const tableW = tableEl.getBoundingClientRect().width || 1;

        const rightLine = document.createElement("div");
        const leftLine = document.createElement("div");
        rightLine.className = leftLine.className = "col-drag-line";
        rightLine.style.top = leftLine.style.top = "0";
        wrapEl.appendChild(rightLine);
        wrapEl.appendChild(leftLine);

        function placeLines(clientX) {
          const wrapLeft = wrapEl.getBoundingClientRect().left;
          const thLeft = tableEl.querySelectorAll("thead th")[gripIdx].getBoundingClientRect().left;
          rightLine.style.left = (clientX - wrapLeft) + "px";
          rightLine.style.height = leftLine.style.height = wrapEl.scrollHeight + "px";
          leftLine.style.left = (thLeft - wrapLeft) + "px";
        }
        placeLines(eDown.clientX);

        function onMove(e) {
          const dPct = ((e.clientX - startX) / tableW) * 100;
          let a = startA + dPct, b = startB - dPct;
          if (a < minPct) { b -= minPct - a; a = minPct; }
          if (b < minPct) { a -= minPct - b; b = minPct; }
          colPcts[gripIdx] = a; colPcts[gripIdx + 1] = b;
          applyColPcts();
          placeLines(e.clientX);
        }
        let done = false;
        function onUp() {
          if (done) return;
          done = true;
          grip.classList.remove("dragging");
          rightLine.remove();
          leftLine.remove();
          localStorage.setItem(colPctsKey, JSON.stringify(colPcts));
          grip.removeEventListener("pointermove", onMove);
          grip.removeEventListener("pointerup", onUp);
          grip.removeEventListener("pointercancel", onUp);
          document.removeEventListener("pointerup", onUp);
          document.removeEventListener("pointercancel", onUp);
        }
        grip.addEventListener("pointermove", onMove);
        grip.addEventListener("pointerup", onUp);
        grip.addEventListener("pointercancel", onUp);
        document.addEventListener("pointerup", onUp);
        document.addEventListener("pointercancel", onUp);
      });
    });
  }

  // Size the table-wrap explicitly so the LIST scrolls internally instead
  // of the dialog body. We measure the actual rendered tab-panel content
  // area (top of body-main to bottom of dialog body). The empirical
  // second-pass overflow correction handles any mismatch (e.g. when the
  // inline search bar pushes the layout).
  const wrapEl = container.querySelector(".engines-table-wrap");
  const bodyMainEl = container.querySelector(".engines-body-main");
  // Ribbon sits below the list, search bar below the ribbon. Size the list
  // so the panel (list + ribbon + search) fills the dialog body exactly.
  const ribbonEl = container.querySelector(".engines-ribbon");
  const searchWrapEl = container.querySelector(".engines-search-wrap");
  function sizeWrap() {
    const dialog = container.closest("wa-dialog");
    const body = dialog?.shadowRoot?.querySelector('[part~="body"]');
    const topAnchor = bodyMainEl.getBoundingClientRect().top;
    const bodyBottom = body
      ? body.getBoundingClientRect().bottom
      : window.innerHeight - 8;
    const ribbonH = ribbonEl?.offsetHeight || 0;
    const searchH = searchWrapEl?.offsetHeight || 0;
    let h = Math.max(120, Math.floor(bodyBottom - topAnchor - ribbonH - searchH - 8));
    wrapEl.style.height = h + "px";
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
  sizeWrapRef = sizeWrap;

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
