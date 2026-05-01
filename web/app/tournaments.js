// Tournaments perspective: master list + "New Tournament" verb.
// Path/defaults configuration lives in the global Settings dialog under
// the "Tournament" tab — not here.
//
// Per-row verbs: Start / Stop / Open workspace / Remove. Click a row's
// main area to inspect its frozen template (read-only). Clicking + New
// Tournament opens a dialog with engine-list builder and template form
// pre-filled from the saved defaults.

import { confirm, reportError, showDialog, toast } from "./dialogs.js";
import { mountTournamentTemplateForm } from "./tournament-template-form.js";
import { closeActiveWorkspace, getActiveWorkspace, openTournamentWorkspace } from "./tournament-workspace.js";

export function mountTournaments({ container, api, events, log, token }) {
  container.innerHTML = `
    <div class="tournaments-panel">
      <menu class="tournaments-menubar">
        <li><button class="tmb-item tournament-new">New</button></li>
        <li class="tmb-menu tmb-window-menu">
          <button class="tmb-item tmb-window-btn">Window</button>
          <ul class="tmb-dropdown">
            <li><button class="tmb-dd-item tmb-tile">Tile</button></li>
            <li><button class="tmb-dd-item tmb-cascade">Cascade</button></li>
            <li class="tmb-separator"></li>
            <li><button class="tmb-dd-item tmb-closeall">Close All</button></li>
          </ul>
        </li>
      </menu>

      <div class="tournaments-empty hidden">
        <p class="empty-message"></p>
      </div>

      <ul class="tournaments-list" role="list"></ul>
    </div>
  `;

  const newBtn = container.querySelector(".tournament-new");
  const windowMenu = container.querySelector(".tmb-window-menu");
  const windowMenuBtn = container.querySelector(".tmb-window-btn");
  const listEl = container.querySelector(".tournaments-list");
  const emptyEl = container.querySelector(".tournaments-empty");
  const emptyMsg = emptyEl.querySelector(".empty-message");

  let tournaments = [];
  let activeId = null;
  let settings = null; // { fastchess_path, tournaments_root, default_template, fastchess_detected }

  // ---- API helpers --------------------------------------------------------

  async function loadSettings() {
    try {
      settings = await api("GET", "/api/tournament-settings");
      renderList();
    } catch (e) {
      reportError({ log }, "Loading tournament settings failed", e);
    }
  }

  async function loadList() {
    try {
      const body = await api("GET", "/api/tournaments");
      tournaments = body.tournaments;
      activeId = body.active_id;
      renderList();
    } catch (e) {
      reportError({ log }, "Loading tournaments failed", e);
    }
  }

  // ---- Rendering ----------------------------------------------------------

  function renderList() {
    listEl.innerHTML = "";

    const noFastchess = !settings || !settings.fastchess_detected;
    const noTournaments = tournaments.length === 0;

    if (noFastchess) {
      emptyEl.classList.remove("hidden");
      emptyMsg.textContent =
        "fastchess not configured — open Settings → Tournament to set the binary path.";
      newBtn.disabled = true;
      return;
    }
    newBtn.disabled = false;

    if (noTournaments) {
      emptyEl.classList.remove("hidden");
      emptyMsg.textContent = "No tournaments yet — click + New Tournament to create one.";
      return;
    }
    emptyEl.classList.add("hidden");

    for (const t of tournaments) {
      listEl.appendChild(renderRow(t));
    }
  }

  function renderRow(t) {
    const li = document.createElement("li");
    li.className = "tournament-row";
    li.dataset.id = t.id;

    const isActive = t.id === activeId;
    const status = t.status;

    li.innerHTML = `
      <div class="tournament-row-main">
        <span class="tournament-name"></span>
        <span class="tournament-id muted"></span>
        <span class="tournament-status status-${status}">${status}</span>
        <span class="tournament-engines muted"></span>
      </div>
      <div class="tournament-row-actions">
        <wa-button class="row-start icon-only" size="small" aria-label="Start" title="Start">
          <wa-icon name="play"></wa-icon>
        </wa-button>
        <wa-button class="row-stop icon-only" size="small" aria-label="Stop" title="Stop">
          <wa-icon name="stop"></wa-icon>
        </wa-button>
        <wa-button class="row-workspace icon-only" size="small" aria-label="Open workspace" title="Open workspace">
          <wa-icon name="window-restore"></wa-icon>
        </wa-button>
        <wa-button class="row-remove icon-only" size="small" aria-label="Remove" title="Remove">
          <wa-icon name="trash"></wa-icon>
        </wa-button>
      </div>
    `;

    li.querySelector(".tournament-name").textContent = t.name;
    li.querySelector(".tournament-id").textContent = t.id.slice(0, 7);
    const engineNames = (t.engines || []).map((e) => e.name).join(", ");
    li.querySelector(".tournament-engines").textContent = engineNames;

    const startBtn = li.querySelector(".row-start");
    const stopBtn = li.querySelector(".row-stop");
    const removeBtn = li.querySelector(".row-remove");
    const workspaceBtn = li.querySelector(".row-workspace");

    const anotherRunning = activeId !== null && !isActive;
    startBtn.disabled = isActive || anotherRunning || status === "running" || status === "done";
    stopBtn.disabled = !isActive;
    removeBtn.disabled = isActive;

    startBtn.addEventListener("click", (ev) => { ev.stopPropagation(); startOne(t); });
    stopBtn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      stopBtn.disabled = true;
      stopBtn.innerHTML = "<wa-spinner></wa-spinner>";
      await stopOne(t);
    });
    removeBtn.addEventListener("click", (ev) => { ev.stopPropagation(); removeOne(t); });
    workspaceBtn.addEventListener("click", (ev) => { ev.stopPropagation(); openWorkspace(t); });

    li.addEventListener("click", () => openInspect(t));

    return li;
  }

  // ---- Verbs --------------------------------------------------------------

  async function startOne(t) {
    try {
      await api("POST", `/api/tournaments/${t.id}/start`);
    } catch (e) {
      reportError({ log }, `Starting "${t.name}" failed`, e);
      return;
    }
    await loadList();
  }

  async function stopOne(t) {
    try {
      await api("POST", `/api/tournaments/${t.id}/stop`);
    } catch (e) {
      reportError({ log }, `Stopping "${t.name}" failed`, e);
      await loadList();
      return;
    }
    await loadList();
  }

  async function removeOne(t) {
    const ok = await confirm({
      message: `Remove "${t.name}"? This deletes its directory and PGN.`,
      okLabel: "Remove",
      destructive: true,
    });
    if (!ok) return;
    try {
      await api("DELETE", `/api/tournaments/${t.id}`);
      toast(`Removed "${t.name}"`, { variant: "neutral" });
    } catch (e) {
      reportError({ log }, `Removing "${t.name}" failed`, e);
      return;
    }
    await loadList();
  }

  function openWorkspace(t) {
    const menubar = container.querySelector(".tournaments-menubar");
    const top = Math.round(menubar.getBoundingClientRect().bottom);
    openTournamentWorkspace({ api, events, log, token, tournament: t, top });
    syncWindowMenu();
  }

  async function openInspect(t) {
    let detail;
    try {
      detail = await api("GET", `/api/tournaments/${t.id}`);
    } catch (e) {
      reportError({ log }, `Loading "${t.name}" failed`, e);
      return;
    }
    await showDialog({
      label: `${t.name} (${detail.status})`,
      width: "min(640px, 92vw)",
      body: (resolve, dialog) => {
        const wrap = document.createElement("div");
        wrap.className = "inspect-form";
        const meta = document.createElement("div");
        meta.className = "inspect-meta muted";
        const eng = (detail.engines || []).map((e) => e.name).join(" vs ");
        meta.textContent = `engines: ${eng || "—"}`;
        wrap.appendChild(meta);

        const formHost = document.createElement("div");
        mountTournamentTemplateForm({
          container: formHost,
          initialValues: detail.template || {},
          readOnly: true,
        });
        wrap.appendChild(formHost);

        const close = document.createElement("wa-button");
        close.slot = "footer";
        close.size = "small";
        close.textContent = "Close";
        close.addEventListener("click", () => resolve());
        dialog.append(wrap, close);
      },
    });
  }

  // ---- New Tournament dialog ---------------------------------------------

  newBtn.addEventListener("click", () => openNewTournamentDialog());

  // ---- Window menu --------------------------------------------------------

  function syncWindowMenu() {
    windowMenuBtn.disabled = !getActiveWorkspace();
  }

  function closeMenus() {
    container.querySelectorAll(".tmb-menu.open").forEach(m => m.classList.remove("open"));
  }

  windowMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (windowMenuBtn.disabled) return;
    const isOpen = windowMenu.classList.contains("open");
    closeMenus();
    if (!isOpen) windowMenu.classList.add("open");
  });

  container.querySelector(".tmb-tile").addEventListener("click", () => {
    closeMenus();
    getActiveWorkspace()?.tile();
  });
  container.querySelector(".tmb-cascade").addEventListener("click", () => {
    closeMenus();
    getActiveWorkspace()?.cascade();
  });
  container.querySelector(".tmb-closeall").addEventListener("click", () => {
    closeMenus();
    getActiveWorkspace()?.closeAll();
    syncWindowMenu();
  });

  document.addEventListener("click", closeMenus);

  async function openNewTournamentDialog() {
    let registry;
    try {
      registry = await api("GET", "/engines");
    } catch (e) {
      reportError({ log }, "Loading engine registry failed", e);
      return;
    }
    const available = registry.engines || [];
    if (available.length < 2) {
      toast("Register at least 2 engines first.", { variant: "danger" });
      return;
    }

    const defaults =
      (settings && settings.default_template) ||
      { tc: "10+0.1", rounds: 10, games_in_parallel: 1 };

    const result = await showDialog({
      label: "New Tournament",
      width: "min(720px, 94vw)",
      defaultValue: null,
      body: (resolve, dialog) => {
        const wrap = document.createElement("div");
        wrap.className = "new-tournament-form";
        wrap.innerHTML = `
          <wa-input class="nt-name" label="Name" size="small" placeholder="my tournament"></wa-input>

          <div class="nt-section">
            <label class="nt-section-label">Engines</label>
            <div class="nt-engine-builder"></div>
          </div>

          <div class="nt-section">
            <label class="nt-section-label">Settings</label>
            <div class="nt-template-host"></div>
          </div>
        `;

        const builderHost = wrap.querySelector(".nt-engine-builder");
        const builder = mountEngineBuilder({
          host: builderHost,
          available,
          initial: [],
        });

        const formHost = wrap.querySelector(".nt-template-host");
        const tplCtl = mountTournamentTemplateForm({
          container: formHost,
          initialValues: defaults,
        });

        const nameInput = wrap.querySelector(".nt-name");

        const create = document.createElement("wa-button");
        create.slot = "footer";
        create.size = "small";
        create.variant = "brand";
        create.textContent = "Create";

        function isValid() {
          return (nameInput.value || "").trim() !== "" && builder.getEngines().length >= 2;
        }

        function refreshValidity() {
          create.disabled = !isValid();
        }
        refreshValidity();
        nameInput.addEventListener("input", refreshValidity);
        builder.onChange(refreshValidity);

        create.addEventListener("click", () => {
          if (!isValid()) return;
          let template;
          try {
            template = tplCtl.getValues();
          } catch (e) {
            toast(e.message, { variant: "danger" });
            return;
          }
          resolve({
            name: nameInput.value.trim(),
            template,
            engines: builder.getEngines(),
          });
        });

        dialog.append(wrap, create);
        requestAnimationFrame(() => nameInput.focus());
      },
    });
    if (!result) return;
    try {
      await api("POST", "/api/tournaments", result);
      toast(`Created "${result.name}"`, { variant: "neutral" });
    } catch (e) {
      reportError({ log }, "Creating tournament failed", e);
      return;
    }
    await loadList();
  }

  // ---- Live updates from WS ----------------------------------------------

  const offEvents = events.on((evt) => {
    if (evt.kind === "tournament_status" || evt.kind === "tournament_update") {
      loadList();
    }
  });

  // Settings can change in another tab/dialog — pick those up too.
  function onSettingsChanged() {
    loadSettings();
  }
  window.addEventListener("sturddle:settings-changed", onSettingsChanged);

  // ---- Initial load -------------------------------------------------------

  syncWindowMenu();

  (async () => {
    await loadSettings();
    await loadList();
  })();

  return {
    unmount() {
      offEvents();
      window.removeEventListener("sturddle:settings-changed", onSettingsChanged);
      document.removeEventListener("click", closeMenus);
      closeActiveWorkspace();
    },
  };
}


// ---------------------------------------------------------------------------
// Two-pane engine builder
//
// Left pane: registry engines not yet picked.
// Right pane: picked engines, in tournament order. Up/Down arrows reorder;
//             Add → / ← Remove move engines between panes.
// ---------------------------------------------------------------------------

function mountEngineBuilder({ host, available, initial = [] }) {
  // Each engine is the registry entry shape: {id, name, path, ...}.
  // We persist `{name, cmd}` pairs in the result (RunSpec-shaped).
  let pickedIds = initial.map((e) => e.id);
  const byId = new Map(available.map((e) => [e.id, e]));
  const listeners = new Set();

  host.innerHTML = `
    <div class="ne-panes">
      <div class="ne-pane ne-pane-available">
        <div class="ne-pane-title">Available</div>
        <ul class="ne-list ne-available-list" role="listbox" tabindex="0"></ul>
      </div>
      <div class="ne-controls">
        <wa-button class="ne-add icon-only" size="small" aria-label="Add" title="Add">
          <wa-icon name="arrow-right"></wa-icon>
        </wa-button>
        <wa-button class="ne-remove icon-only" size="small" aria-label="Remove" title="Remove">
          <wa-icon name="arrow-left"></wa-icon>
        </wa-button>
      </div>
      <div class="ne-pane ne-pane-picked">
        <div class="ne-pane-title">Selected</div>
        <ul class="ne-list ne-picked-list" role="listbox" tabindex="0"></ul>
        <div class="ne-reorder">
          <wa-button class="ne-up icon-only" size="small" aria-label="Move up" title="Move up">
            <wa-icon name="arrow-up"></wa-icon>
          </wa-button>
          <wa-button class="ne-down icon-only" size="small" aria-label="Move down" title="Move down">
            <wa-icon name="arrow-down"></wa-icon>
          </wa-button>
        </div>
      </div>
    </div>
  `;

  const availableList = host.querySelector(".ne-available-list");
  const pickedList = host.querySelector(".ne-picked-list");
  const addBtn = host.querySelector(".ne-add");
  const removeBtn = host.querySelector(".ne-remove");
  const upBtn = host.querySelector(".ne-up");
  const downBtn = host.querySelector(".ne-down");

  let availableSelectedId = null;
  let pickedSelectedId = null;

  function render() {
    availableList.innerHTML = "";
    for (const e of available) {
      if (pickedIds.includes(e.id)) continue;
      const li = document.createElement("li");
      li.className = "ne-item" + (e.id === availableSelectedId ? " selected" : "");
      li.dataset.id = e.id;
      li.textContent = e.name;
      li.addEventListener("click", () => {
        availableSelectedId = e.id;
        render();
      });
      li.addEventListener("dblclick", () => doAdd(e.id));
      availableList.appendChild(li);
    }

    pickedList.innerHTML = "";
    for (const id of pickedIds) {
      const e = byId.get(id);
      if (!e) continue;
      const li = document.createElement("li");
      li.className = "ne-item" + (id === pickedSelectedId ? " selected" : "");
      li.dataset.id = id;
      li.textContent = e.name;
      li.addEventListener("click", () => {
        pickedSelectedId = id;
        render();
      });
      li.addEventListener("dblclick", () => doRemove(id));
      pickedList.appendChild(li);
    }

    addBtn.disabled = !availableSelectedId;
    removeBtn.disabled = !pickedSelectedId;
    const idx = pickedSelectedId ? pickedIds.indexOf(pickedSelectedId) : -1;
    upBtn.disabled = idx <= 0;
    downBtn.disabled = idx < 0 || idx >= pickedIds.length - 1;
  }

  function doAdd(id) {
    if (!id || pickedIds.includes(id)) return;
    pickedIds.push(id);
    pickedSelectedId = id;
    availableSelectedId = null;
    render();
    notify();
  }

  function doRemove(id) {
    if (!id) return;
    const idx = pickedIds.indexOf(id);
    if (idx < 0) return;
    pickedIds.splice(idx, 1);
    pickedSelectedId = pickedIds[Math.min(idx, pickedIds.length - 1)] || null;
    render();
    notify();
  }

  function move(delta) {
    if (!pickedSelectedId) return;
    const idx = pickedIds.indexOf(pickedSelectedId);
    const target = idx + delta;
    if (target < 0 || target >= pickedIds.length) return;
    [pickedIds[idx], pickedIds[target]] = [pickedIds[target], pickedIds[idx]];
    render();
    notify();
  }

  addBtn.addEventListener("click", () => doAdd(availableSelectedId));
  removeBtn.addEventListener("click", () => doRemove(pickedSelectedId));
  upBtn.addEventListener("click", () => move(-1));
  downBtn.addEventListener("click", () => move(1));

  function notify() {
    for (const fn of listeners) fn();
  }

  render();

  return {
    getEngines() {
      return pickedIds.map((id) => {
        const e = byId.get(id);
        return { name: e.name, cmd: e.path };
      });
    },
    onChange(fn) { listeners.add(fn); },
  };
}
