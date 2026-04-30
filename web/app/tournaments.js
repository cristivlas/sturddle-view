// Tournaments panel: master list of saved tournaments with Start / Stop /
// Remove / Open workspace verbs. Settings strip on top (fastchess path,
// tournaments root, default-template button). New Tournament dialog opens
// from the + button.
//
// Slice 6 ships the v0: list + create + start/stop/remove + live status
// badges. Slice 7 replaces the inline create form with the reusable
// template-form component. Slice 8 wires Open workspace to WinBox.

import { confirm, pickFile, reportError, showDialog, toast } from "./dialogs.js";
import { mountTournamentTemplateForm } from "./tournament-template-form.js";

export function mountTournaments({ container, api, events, log }) {
  container.innerHTML = `
    <div class="tournaments-panel">
      <div class="tournaments-settings">
        <div class="settings-row">
          <label>fastchess binary</label>
          <span class="path-display path-fastchess muted">(not set)</span>
          <wa-button class="settings-pick-fastchess" size="small">Browse…</wa-button>
        </div>
        <div class="settings-row">
          <label>tournaments root</label>
          <span class="path-display path-root muted">(default)</span>
          <wa-button class="settings-pick-root" size="small">Browse…</wa-button>
        </div>
        <div class="settings-row settings-defaults-row">
          <label>defaults</label>
          <span class="defaults-summary muted">used as the template for new tournaments</span>
          <wa-button class="settings-edit-defaults" size="small">Edit…</wa-button>
        </div>
      </div>

      <div class="tournaments-toolbar">
        <wa-button class="tournament-new" size="small" variant="brand">
          <wa-icon name="plus" slot="prefix"></wa-icon> New Tournament
        </wa-button>
      </div>

      <div class="tournaments-empty hidden">
        <p class="empty-message"></p>
      </div>

      <ul class="tournaments-list" role="list"></ul>
    </div>
  `;

  const fastchessPath = container.querySelector(".path-fastchess");
  const rootPath = container.querySelector(".path-root");
  const fastchessBtn = container.querySelector(".settings-pick-fastchess");
  const rootBtn = container.querySelector(".settings-pick-root");
  const editDefaultsBtn = container.querySelector(".settings-edit-defaults");
  const newBtn = container.querySelector(".tournament-new");
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
      renderSettings();
    } catch (e) {
      reportError({ log }, "Loading tournament settings failed", e);
    }
  }

  async function saveSettings(patch) {
    try {
      settings = await api("PUT", "/api/tournament-settings", patch);
      renderSettings();
      renderList();
    } catch (e) {
      reportError({ log }, "Saving tournament settings failed", e);
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

  function renderSettings() {
    if (!settings) return;
    fastchessPath.textContent =
      settings.fastchess_detected || settings.fastchess_path || "(not set)";
    fastchessPath.classList.toggle("muted", !settings.fastchess_detected);
    fastchessPath.classList.toggle("error", !settings.fastchess_detected);
    rootPath.textContent = settings.tournaments_root || "(default)";
  }

  function renderList() {
    listEl.innerHTML = "";

    const noFastchess = !settings || !settings.fastchess_detected;
    const noTournaments = tournaments.length === 0;

    if (noFastchess) {
      emptyEl.classList.remove("hidden");
      emptyMsg.textContent = "fastchess not found — set the path above to enable tournaments.";
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
        <span class="tournament-status status-${status}">${status}</span>
      </div>
      <div class="tournament-row-meta muted"></div>
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
    const meta = li.querySelector(".tournament-row-meta");
    const engineNames = (t.engines || []).map((e) => e.name).join(", ");
    meta.textContent = engineNames ? `engines: ${engineNames}` : "";

    const startBtn = li.querySelector(".row-start");
    const stopBtn = li.querySelector(".row-stop");
    const removeBtn = li.querySelector(".row-remove");
    const workspaceBtn = li.querySelector(".row-workspace");

    // Verbs enable/disable per status. Single-active is enforced server-side
    // but we also reflect it in the UI: while one is running, others can't start.
    const anotherRunning = activeId !== null && !isActive;
    startBtn.disabled = isActive || anotherRunning || status === "running" || status === "done";
    stopBtn.disabled = !isActive;
    removeBtn.disabled = isActive;

    startBtn.addEventListener("click", (ev) => { ev.stopPropagation(); startOne(t); });
    stopBtn.addEventListener("click", (ev) => { ev.stopPropagation(); stopOne(t); });
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
    // Slice 8 wires WinBox windows here.
    toast(`Workspace for "${t.name}" — coming soon`, { variant: "neutral" });
  }

  async function openInspect(t) {
    // Read-only render of the frozen template. The template object on the
    // list row may be stale; the GET endpoint is authoritative and also
    // returns standings/SPRT once games exist.
    let detail;
    try {
      detail = await api("GET", `/api/tournaments/${t.id}`);
    } catch (e) {
      reportError({ log }, `Loading "${t.name}" failed`, e);
      return;
    }
    await showDialog({
      label: `${t.name} (${detail.status})`,
      width: "min(560px, 92vw)",
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

  // ---- Settings actions ---------------------------------------------------

  fastchessBtn.addEventListener("click", async () => {
    const path = await pickFile({ api, mode: "executable", title: "Pick fastchess binary" });
    if (path) await saveSettings({ fastchess_path: path });
  });

  rootBtn.addEventListener("click", async () => {
    const path = await pickFile({ api, mode: "directory", title: "Pick tournaments root" });
    if (path) await saveSettings({ tournaments_root: path });
  });

  editDefaultsBtn.addEventListener("click", () => openDefaultsDialog());

  async function openDefaultsDialog() {
    const initial = (settings && settings.default_template) || {};
    const result = await showDialog({
      label: "Tournament defaults",
      width: "min(560px, 92vw)",
      defaultValue: null,
      body: (resolve, dialog) => {
        const formHost = document.createElement("div");
        const ctl = mountTournamentTemplateForm({
          container: formHost,
          initialValues: initial,
        });

        const cancel = document.createElement("wa-button");
        cancel.slot = "footer";
        cancel.size = "small";
        cancel.textContent = "Cancel";
        cancel.addEventListener("click", () => resolve(null));

        const save = document.createElement("wa-button");
        save.slot = "footer";
        save.size = "small";
        save.variant = "brand";
        save.textContent = "Save";
        save.addEventListener("click", () => {
          try {
            resolve(ctl.getValues());
          } catch (e) {
            toast(e.message, { variant: "danger" });
          }
        });
        dialog.append(formHost, cancel, save);
      },
    });
    if (!result) return;
    await saveSettings({ default_template: result });
    toast("Defaults saved", { variant: "neutral" });
  }

  // ---- New Tournament dialog ---------------------------------------------

  newBtn.addEventListener("click", () => openNewTournamentDialog());

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
      toast("Need at least 2 registered engines to create a tournament.", {
        variant: "danger",
      });
      return;
    }

    const defaults = (settings && settings.default_template) || { tc: "10+0.1", rounds: 10, games_in_parallel: 1 };

    const result = await showDialog({
      label: "New Tournament",
      width: "min(560px, 92vw)",
      defaultValue: null,
      body: (resolve, dialog) => {
        const wrap = document.createElement("div");
        wrap.className = "new-tournament-form";
        wrap.innerHTML = `
          <wa-input class="nt-name" label="Name" size="small" placeholder="my tournament" required></wa-input>

          <div class="nt-section">
            <label>Engines (pick 2 or more)</label>
            <ul class="nt-engines"></ul>
          </div>

          <div class="nt-template-host"></div>
        `;

        const enginesList = wrap.querySelector(".nt-engines");
        for (const e of available) {
          const li = document.createElement("li");
          li.innerHTML = `
            <wa-checkbox value="${e.id}" data-name="${e.name}" data-cmd="${e.path}">
              ${e.name}
            </wa-checkbox>`;
          enginesList.appendChild(li);
        }

        const formHost = wrap.querySelector(".nt-template-host");
        const tplCtl = mountTournamentTemplateForm({
          container: formHost,
          initialValues: defaults,
        });

        const nameInput = wrap.querySelector(".nt-name");

        const cancel = document.createElement("wa-button");
        cancel.slot = "footer";
        cancel.size = "small";
        cancel.textContent = "Cancel";
        cancel.addEventListener("click", () => resolve(null));

        const create = document.createElement("wa-button");
        create.slot = "footer";
        create.size = "small";
        create.variant = "brand";
        create.textContent = "Create";
        create.addEventListener("click", () => {
          const checks = enginesList.querySelectorAll("wa-checkbox[checked]");
          const picked = [...checks].map((c) => ({
            name: c.dataset.name,
            cmd: c.dataset.cmd,
          }));
          if (picked.length < 2) {
            toast("Pick at least 2 engines.", { variant: "danger" });
            return;
          }
          let template;
          try {
            template = tplCtl.getValues();
          } catch (e) {
            toast(e.message, { variant: "danger" });
            return;
          }
          resolve({
            name: (nameInput.value || "").trim() || "tournament",
            template,
            engines: picked,
          });
        });

        dialog.append(wrap, cancel, create);
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
      // Cheap and correct: refetch the list. Volume is low (one per
      // start/stop/done plus per-game finishes), and the row data
      // includes computed fields we'd otherwise have to recompute here.
      loadList();
    }
  });

  // ---- Initial load -------------------------------------------------------

  (async () => {
    await loadSettings();
    await loadList();
  })();

  return {
    unmount() {
      offEvents();
    },
  };
}
