// Engines panel: search + scrollable list with a left-side vertical
// ribbon mirroring the Play and Tournaments perspectives. Ribbon
// holds Add, sort A→Z / Z→A, and the row-targeted Use / Options /
// Remove actions (which act on the focused list row).

import { apiErrorDetail, confirm, pickFile, reportError, toast } from "./dialogs.js";
import { showEngineOptionsDialog } from "./engine-options-dialog.js";


export function mountEngines({ container, api }) {
  container.innerHTML = `
    <div class="engines-panel">
      <div class="engines-ribbon" role="toolbar" aria-label="Engine actions">
        <button class="ribbon-btn engines-add" aria-label="Add engine" title="Add engine">
          <wa-icon name="plus"></wa-icon>
        </button>
        <button class="ribbon-btn engines-detail-use" disabled aria-label="Use as active engine" title="Use as active engine">
          <wa-icon name="check"></wa-icon>
        </button>
        <button class="ribbon-btn engines-detail-options" disabled aria-label="UCI options" title="UCI options">
          <wa-icon name="sliders"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn engines-search-btn" aria-label="Search engines" title="Search engines">
          <wa-icon name="magnifying-glass"></wa-icon>
        </button>
        <button class="ribbon-btn engines-sort-asc" aria-label="Sort A→Z" title="Sort A→Z">
          <wa-icon name="arrow-down-a-z"></wa-icon>
        </button>
        <button class="ribbon-btn engines-sort-desc" aria-label="Sort Z→A" title="Sort Z→A">
          <wa-icon name="arrow-down-z-a"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn ribbon-btn--danger engines-detail-remove" disabled aria-label="Remove engine" title="Remove engine">
          <wa-icon name="trash"></wa-icon>
        </button>
      </div>

      <div class="engines-body-main">
        <div class="engines-body-content">
          <div class="engines-search-wrap">
            <wa-input class="engines-search" size="small" placeholder="Search engines…" clearable autocomplete="off"></wa-input>
          </div>

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
    </div>
  `;

  const detailUseBtn = container.querySelector(".engines-detail-use");
  const detailRemoveBtn = container.querySelector(".engines-detail-remove");
  const detailOptionsBtn = container.querySelector(".engines-detail-options");
  const addBtn = container.querySelector(".engines-add");
  const sortAscBtn = container.querySelector(".engines-sort-asc");
  const sortDescBtn = container.querySelector(".engines-sort-desc");
  const searchBtn = container.querySelector(".engines-search-btn");
  const searchWrap = container.querySelector(".engines-search-wrap");
  const searchInput = container.querySelector(".engines-search");
  const list = container.querySelector(".engines-list");
  const emptyEl = container.querySelector(".engines-empty");
  const emptyMsg = emptyEl.querySelector(".empty-message");

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
      emptyMsg.append("No engines yet — click ", addLink, " to add one.");
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

  function lockedMsg(e) {
    if (!e?.locked?.length) return null;
    const list = e.locked.map((t) => `${t.name} (${t.status})`).join(", ");
    return `${e.name} is used by: ${list}`;
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
    searchBtn.classList.toggle("is-active", !!filterText);
    renderList();
  });

  function closeSearch() {
    searchWrap.classList.remove("open");
    searchBtn.classList.remove("is-active");
    searchInput.value = "";
    filterText = "";
    renderList();
    document.removeEventListener("pointerdown", onOutsideClick);
    document.removeEventListener("keydown", onSearchKey);
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
      const btnRect = searchBtn.getBoundingClientRect();
      const ribbonRect = searchBtn.closest(".engines-ribbon").getBoundingClientRect();
      searchWrap.style.left = ribbonRect.right + "px";
      searchWrap.style.top = (btnRect.top + btnRect.height / 2) + "px";
      searchWrap.style.transform = "translateY(-50%)";
      searchWrap.offsetWidth; // force reflow so position is committed before transition starts
      searchWrap.classList.add("open");
      searchBtn.classList.add("is-active");
      searchInput.focus();
      document.addEventListener("pointerdown", onOutsideClick);
      document.addEventListener("keydown", onSearchKey);
    } else {
      closeSearch();
    }
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
    const e = engines.find((x) => x.id === selectedDetailId);
    if (!e) return;
    const msg = lockedMsg(e);
    if (msg) { toast(msg, { variant: "warning" }); return; }
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
    let engine = engines.find((x) => x.id === selectedDetailId);
    const msg = lockedMsg(engine);
    if (msg) { toast(msg, { variant: "warning" }); return; }
    // If we have no cached UCI options, try one auto re-probe before
    // opening — heals the case where the original add-time probe failed
    // (e.g. transient spawn error) so the user doesn't see an empty dialog
    // and have to click Refresh themselves.
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
      if (result === null) break; // dismissed without saving
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
        // Engine row was created but the UCI probe failed — warn so the
        // user knows why the per-engine options dialog will be empty.
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

  // Column resize (same pattern as dashboard)
  const COL_PCTS_KEY = "sturddle.engines.colPcts3";
  const colEls = Array.from(container.querySelectorAll(".engines-table col"));
  const DEFAULT_PCTS = [20, 8, 72];
  let colPcts = DEFAULT_PCTS.slice();

  function applyColPcts() {
    colEls.forEach((c, i) => { c.style.width = colPcts[i] + "%"; });
  }
  try {
    const saved = JSON.parse(localStorage.getItem(COL_PCTS_KEY));
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
      function onUp() {
        grip.classList.remove("dragging");
        rightLine.remove();
        leftLine.remove();
        localStorage.setItem(COL_PCTS_KEY, JSON.stringify(colPcts));
        grip.removeEventListener("pointermove", onMove);
        grip.removeEventListener("pointerup", onUp);
        grip.removeEventListener("pointercancel", onUp);
      }
      grip.addEventListener("pointermove", onMove);
      grip.addEventListener("pointerup", onUp);
      grip.addEventListener("pointercancel", onUp);
    });
  });

  refresh();
  return { refresh };
}
