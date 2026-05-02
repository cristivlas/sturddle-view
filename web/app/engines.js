// Engines panel: search + scrollable list with a left-side vertical
// ribbon mirroring the Play and Tournaments perspectives. Ribbon
// holds Add, sort A→Z / Z→A, and the row-targeted Use / Options /
// Remove actions (which act on the focused list row).

import { confirm, pickFile, toast } from "./dialogs.js";
import { showEngineOptionsDialog } from "./engine-options-dialog.js";

export function mountEngines({ container, api, onError }) {
  container.innerHTML = `
    <div class="engines-panel">
      <div class="engines-ribbon" role="toolbar" aria-label="Engine actions">
        <button class="ribbon-btn engines-add" aria-label="Add engine" title="Add engine">
          <wa-icon name="plus"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn engines-sort-asc" aria-label="Sort A→Z" title="Sort A→Z">
          <wa-icon name="arrow-down-a-z"></wa-icon>
        </button>
        <button class="ribbon-btn engines-sort-desc" aria-label="Sort Z→A" title="Sort Z→A">
          <wa-icon name="arrow-down-z-a"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn engines-detail-use" disabled aria-label="Use as active engine" title="Use as active engine">
          <wa-icon name="check"></wa-icon>
        </button>
        <button class="ribbon-btn engines-detail-options" disabled aria-label="UCI options" title="UCI options">
          <wa-icon name="sliders"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn ribbon-btn--danger engines-detail-remove" disabled aria-label="Remove engine" title="Remove engine">
          <wa-icon name="trash"></wa-icon>
        </button>
      </div>

      <div class="engines-body-main">
        <div class="engines-body-content">
          <wa-input class="engines-search" size="small" placeholder="Search engines…" clearable>
            <wa-icon slot="start" name="magnifying-glass"></wa-icon>
          </wa-input>

          <ul class="engines-list" role="listbox" tabindex="0"></ul>
        </div>
      </div>
    </div>
  `;

  const detailUseBtn = container.querySelector(".engines-detail-use");
  const detailRemoveBtn = container.querySelector(".engines-detail-remove");
  const detailOptionsBtn = container.querySelector(".engines-detail-options");
  const addBtn = container.querySelector(".engines-add");
  const sortAscBtn = container.querySelector(".engines-sort-asc");
  const sortDescBtn = container.querySelector(".engines-sort-desc");
  const searchInput = container.querySelector(".engines-search");
  const list = container.querySelector(".engines-list");

  const SORT_KEY_LS = "sturddle.engines.sortOrder";
  let engines = [];
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
      onError?.(`engines: ${e.message}`);
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

    if (engines.length === 0) {
      const li = document.createElement("li");
      li.className = "engines-list-empty muted";
      li.textContent = "No engines yet — click + to add one.";
      list.appendChild(li);
      return;
    }
    if (visible.length === 0) {
      const li = document.createElement("li");
      li.className = "engines-list-empty muted";
      li.textContent = "No engines match.";
      list.appendChild(li);
      return;
    }

    for (const e of visible) {
      const li = document.createElement("li");
      li.className = "engines-list-item";
      li.dataset.engineId = e.id;
      if (e.id === selectedDetailId) li.classList.add("focused");
      if (e.id === activeId) li.classList.add("active");

      const name = document.createElement("span");
      name.className = "engines-list-name";
      name.textContent = e.name;
      li.appendChild(name);

      if (e.id === activeId) {
        const badge = document.createElement("wa-icon");
        badge.name = "check";
        badge.className = "engines-list-active-badge";
        li.appendChild(badge);
      }

      li.addEventListener("click", () => {
        selectedDetailId = e.id;
        renderAll();
      });

      list.appendChild(li);
    }
  }

  function syncDetailButtons() {
    const e = engines.find((x) => x.id === selectedDetailId);
    const has = !!e;
    detailOptionsBtn.disabled = !has;
    detailRemoveBtn.disabled = !has;
    detailUseBtn.disabled = !has || (e && e.id === activeId);
  }

  searchInput.addEventListener("input", () => {
    filterText = searchInput.value || "";
    renderList();
  });

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

  detailUseBtn.addEventListener("click", async () => {
    if (!selectedDetailId) return;
    try {
      await api("POST", `/engines/${selectedDetailId}/select`);
      refresh();
    } catch (err) {
      onError?.(`select: ${err.message}`);
    }
  });

  detailRemoveBtn.addEventListener("click", async () => {
    if (!selectedDetailId) return;
    const e = engines.find((x) => x.id === selectedDetailId);
    if (!e) return;
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
      onError?.(`remove: ${err.message}`);
    }
  });

  detailOptionsBtn.addEventListener("click", async () => {
    if (!selectedDetailId) return;
    let engine = engines.find((x) => x.id === selectedDetailId);
    while (engine) {
      const result = await showEngineOptionsDialog({ engine, api });
      if (result === null) break; // dismissed without saving
      if (result?.__refresh || result?.__reopen) {
        engine = result.engine;
        continue;
      }
      refresh();
      break;
    }
  });

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
      onError?.(`add: ${e.message}`);
    }
  });

  refresh();
  return { refresh };
}
