// Engines panel: stacked layout (header above list).
// Top row: name + path on the left, actions on the right.
// `+` Add is always present; star / sliders / trash appear only when a
// selected engine is highlighted.
// Below: search box + scrollable list.

import { confirm, pickFile, toast } from "./dialogs.js";

export function mountEngines({ container, api, onError }) {
  container.innerHTML = `
    <div class="engines-panel">
      <header class="engines-header">
        <div class="engines-header-info">
          <h3 class="engines-detail-name"></h3>
        </div>
        <div class="engines-header-actions">
          <wa-button class="engines-detail-use icon-only" size="small" variant="brand"
                     aria-label="Use as active engine" hidden>
            <wa-icon name="star"></wa-icon>
          </wa-button>
          <wa-button class="engines-detail-options icon-only" size="small"
                     aria-label="UCI options" hidden>
            <wa-icon name="sliders"></wa-icon>
          </wa-button>
          <wa-button class="engines-detail-remove icon-only" size="small"
                     aria-label="Remove engine" hidden>
            <wa-icon name="trash"></wa-icon>
          </wa-button>
          <wa-button class="engines-add icon-only" size="small" variant="brand"
                     aria-label="Add engine">
            <wa-icon name="plus"></wa-icon>
          </wa-button>
        </div>
      </header>

      <wa-input class="engines-search" size="small" placeholder="Search engines…" clearable>
        <wa-icon slot="start" name="magnifying-glass"></wa-icon>
      </wa-input>

      <ul class="engines-list" role="listbox" tabindex="0"></ul>
    </div>
  `;

  const headerInfo = container.querySelector(".engines-header-info");
  const detailName = container.querySelector(".engines-detail-name");
  const detailUseBtn = container.querySelector(".engines-detail-use");
  const detailRemoveBtn = container.querySelector(".engines-detail-remove");
  const detailOptionsBtn = container.querySelector(".engines-detail-options");
  const addBtn = container.querySelector(".engines-add");
  const searchInput = container.querySelector(".engines-search");
  const list = container.querySelector(".engines-list");

  let engines = [];
  let selectedDetailId = null;
  let activeId = null;
  let filterText = "";

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
    renderHeader();
  }

  function renderList() {
    list.innerHTML = "";
    const needle = filterText.trim().toLowerCase();
    const visible = needle
      ? engines.filter((e) => e.name.toLowerCase().includes(needle))
      : engines;

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
        badge.name = "star";
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

  function renderHeader() {
    const e = engines.find((x) => x.id === selectedDetailId);
    const has = !!e;

    headerInfo.classList.toggle("empty", !has);

    detailName.textContent = has ? e.name : "";

    // Detail-only actions: hidden when no engine is selected.
    for (const btn of [detailUseBtn, detailOptionsBtn, detailRemoveBtn]) {
      btn.hidden = !has;
    }
    if (has) {
      if (e.id === activeId) detailUseBtn.setAttribute("disabled", "");
      else detailUseBtn.removeAttribute("disabled");
    }
  }

  searchInput.addEventListener("input", () => {
    filterText = searchInput.value || "";
    renderList();
  });

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

  detailOptionsBtn.addEventListener("click", () => {
    toast("UCI options dialog: coming soon", { variant: "neutral" });
  });

  addBtn.addEventListener("click", async () => {
    const path = await pickFile({
      api,
      title: "Pick engine binary",
      mode: "executable",
    });
    if (!path) return;
    const name = path.split(/[\\/]/).pop();
    try {
      const created = await api("POST", "/engines", { name, path });
      toast(`Added ${name}`, { variant: "success" });
      selectedDetailId = created.id;
      refresh();
    } catch (e) {
      onError?.(`add: ${e.message}`);
    }
  });

  refresh();
  return { refresh };
}
