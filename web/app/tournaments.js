// Tournaments perspective: master list + "New Tournament" verb.
// Path/defaults configuration lives in the global Settings dialog under
// the "Tournament" tab — not here.
//
// Row-targeted verbs (Start/Resume, Pause, Open workspace, Info, Remove)
// live in a left-side vertical ribbon that mirrors the Play perspective's
// look and feel. Clicking a row selects it; ribbon actions target the
// selected tournament. New / Sort / Window remain in the top menubar.

import { confirm, reportError, showDialog, toast } from "./dialogs.js";
import { mountTournamentTemplateForm } from "./tournament-template-form.js";
import { getActiveWorkspace, openTournamentWorkspace } from "./tournament-workspace.js";

export function mountTournaments({ container, api, events, log, token }) {
  container.innerHTML = `
    <div class="tournaments-panel">
      <menu class="tournaments-menubar">
        <div class="tournaments-menubar-progress" aria-hidden="true"></div>
        <li class="tmb-menu tmb-sort-menu">
          <button class="tmb-item tmb-sort-btn">Sort</button>
          <ul class="tmb-dropdown">
            <li><button class="tmb-dd-item tmb-sort-opt" data-sort="name">Name</button></li>
            <li><button class="tmb-dd-item tmb-sort-opt" data-sort="status">Status</button></li>
            <li><button class="tmb-dd-item tmb-sort-opt" data-sort="created_at">Created</button></li>
            <li><button class="tmb-dd-item tmb-sort-opt" data-sort="started_at">Started</button></li>
          </ul>
        </li>
        <li class="tmb-menu tmb-window-menu">
          <button class="tmb-item tmb-window-btn">Window</button>
          <ul class="tmb-dropdown">
            <li><button class="tmb-dd-item tmb-tile">Tile</button></li>
            <li><button class="tmb-dd-item tmb-cascade">Cascade</button></li>
            <li><button class="tmb-dd-item tmb-minall">Minimize All</button></li>
            <li class="tmb-separator"></li>
            <li><button class="tmb-dd-item tmb-closeall">Close All</button></li>
          </ul>
        </li>
      </menu>

      <div class="tournaments-body">
        <div class="tournaments-ribbon" role="toolbar" aria-label="Tournament actions">
          <button class="ribbon-btn t-new" aria-label="New tournament" title="New tournament">
            <wa-icon name="plus"></wa-icon>
          </button>
          <span class="ribbon-sep" aria-hidden="true"></span>
          <button class="ribbon-btn t-start" disabled aria-label="Start" title="Start">
            <wa-icon class="t-start-icon" name="play"></wa-icon>
          </button>
          <button class="ribbon-btn t-stop" disabled aria-label="Pause" title="Pause">
            <wa-icon name="pause"></wa-icon>
          </button>
          <span class="ribbon-sep" aria-hidden="true"></span>
          <button class="ribbon-btn t-workspace" disabled aria-label="Open workspace" title="Open workspace">
            <wa-icon name="window-restore"></wa-icon>
          </button>
          <button class="ribbon-btn t-info" disabled aria-label="Info" title="Info">
            <wa-icon name="circle-info"></wa-icon>
          </button>
          <span class="ribbon-sep" aria-hidden="true"></span>
          <button class="ribbon-btn ribbon-btn--danger t-remove" disabled aria-label="Remove" title="Remove">
            <wa-icon name="trash"></wa-icon>
          </button>
        </div>

        <div class="tournaments-body-main">
          <div class="tournaments-empty hidden">
            <p class="empty-message"></p>
          </div>

          <ul class="tournaments-list" role="listbox" tabindex="0"></ul>
        </div>
      </div>
    </div>
  `;

  const newBtn = container.querySelector(".t-new");
  const windowMenu = container.querySelector(".tmb-window-menu");
  const windowMenuBtn = container.querySelector(".tmb-window-btn");
  const sortMenu = container.querySelector(".tmb-sort-menu");
  const sortMenuBtn = container.querySelector(".tmb-sort-btn");
  const listEl = container.querySelector(".tournaments-list");
  const emptyEl = container.querySelector(".tournaments-empty");
  const emptyMsg = emptyEl.querySelector(".empty-message");

  const ribbonStartBtn = container.querySelector(".t-start");
  const ribbonStartIcon = container.querySelector(".t-start-icon");
  const ribbonStopBtn = container.querySelector(".t-stop");
  const ribbonWorkspaceBtn = container.querySelector(".t-workspace");
  const ribbonInfoBtn = container.querySelector(".t-info");
  const ribbonRemoveBtn = container.querySelector(".t-remove");

  const SORT_KEY_LS = "sturddle.tournaments.sortBy";
  const VALID_SORTS = new Set(["name", "status", "created_at", "started_at"]);
  let sortBy = VALID_SORTS.has(localStorage.getItem(SORT_KEY_LS))
    ? localStorage.getItem(SORT_KEY_LS) : "created_at";

  let tournaments = [];
  let activeId = null;
  let selectedId = null;
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
      selectedId = null;
      syncRibbon();
      return;
    }
    newBtn.disabled = false;

    if (noTournaments) {
      emptyEl.classList.remove("hidden");
      emptyMsg.textContent = "No tournaments yet — click + New Tournament to create one.";
      selectedId = null;
      syncRibbon();
      return;
    }
    emptyEl.classList.add("hidden");

    const sorted = sortedTournaments();
    if (!selectedId || !sorted.some((t) => t.id === selectedId)) {
      selectedId = (activeId && sorted.some((t) => t.id === activeId)) ? activeId : sorted[0].id;
    }
    for (const t of sorted) {
      listEl.appendChild(renderRow(t));
    }
    syncMenubarProgress(sorted);
    syncRibbon();
  }

  function syncMenubarProgress(sorted) {
    const strip = container.querySelector(".tournaments-menubar-progress");
    if (!strip) return;
    const running = sorted.find((t) => t.status === "running");
    if (!running) {
      strip.style.width = "0%";
      return;
    }
    const played = running.standings?.games ?? 0;
    const total = totalGames(running);
    const pct = total ? Math.min(100, (played / total) * 100) : 0;
    strip.style.width = pct + "%";
  }

  function sortedTournaments() {
    const arr = tournaments.slice();
    const cmp = (a, b) => {
      const av = a[sortBy] ?? "";
      const bv = b[sortBy] ?? "";
      if (av === bv) return a.created_at.localeCompare(b.created_at);
      // Empty values sink to the bottom for time-based sorts.
      if (av === "") return 1;
      if (bv === "") return -1;
      if (sortBy === "name") return av.localeCompare(bv, undefined, { sensitivity: "base" });
      return av < bv ? -1 : 1;
    };
    return arr.sort(cmp);
  }

  function renderRow(t) {
    const li = document.createElement("li");
    li.className = "tournament-row" + (t.id === selectedId ? " selected" : "");
    li.dataset.id = t.id;

    const status = t.status;
    const isRunning = status === "running";
    const played = t.standings?.games ?? 0;
    const total = totalGames(t);
    const pct = total ? Math.min(100, Math.round((played / total) * 100)) : 0;

    let trailing = "";
    if (isRunning && total) {
      trailing = `
        <div class="tournament-progress" role="progressbar"
             aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${played}">
          <div class="tournament-progress-fill" style="width: ${pct}%"></div>
          <span class="tournament-progress-label">${played} / ${total} · ${pct}%</span>
        </div>
      `;
    } else {
      trailing = `<span class="tournament-engines muted"></span>`;
    }

    li.innerHTML = `
      <div class="tournament-row-main">
        <span class="tournament-status status-${status}">${status}</span>
        <span class="tournament-name"></span>
        ${trailing}
      </div>
    `;

    li.querySelector(".tournament-name").textContent = t.name;
    if (!isRunning || !total) {
      const engineNames = (t.engines || []).map((e) => e.name).join(", ");
      li.querySelector(".tournament-engines").textContent = engineNames;
    }

    li.addEventListener("click", () => {
      listEl.focus({ preventScroll: true });
      if (selectedId === t.id) return;
      selectedId = t.id;
      for (const el of listEl.querySelectorAll(".tournament-row.selected")) {
        el.classList.remove("selected");
      }
      li.classList.add("selected");
      syncRibbon();
    });
    li.addEventListener("dblclick", () => openInfoDialog(t));

    return li;
  }

  function selectedTournament() {
    return tournaments.find((t) => t.id === selectedId) || null;
  }

  function syncRibbon() {
    const t = selectedTournament();
    if (!t) {
      ribbonStartBtn.disabled = true;
      ribbonStopBtn.disabled = true;
      ribbonWorkspaceBtn.disabled = true;
      ribbonInfoBtn.disabled = true;
      ribbonRemoveBtn.disabled = true;
      ribbonStartIcon.setAttribute("name", "play");
      ribbonStartBtn.setAttribute("aria-label", "Start");
      ribbonStartBtn.setAttribute("title", "Start");
      return;
    }
    const isActive = t.id === activeId;
    const anotherRunning = activeId !== null && !isActive;
    const status = t.status;
    const isResume = status === "stopped";

    ribbonStartBtn.disabled = isActive || anotherRunning || status === "running" || status === "done";
    ribbonStopBtn.disabled = !isActive;
    ribbonRemoveBtn.disabled = isActive;
    ribbonWorkspaceBtn.disabled = false;
    ribbonInfoBtn.disabled = false;

    ribbonStartIcon.setAttribute("name", isResume ? "forward-step" : "play");
    const startLabel = isResume ? "Resume" : "Start";
    ribbonStartBtn.setAttribute("aria-label", startLabel);
    ribbonStartBtn.setAttribute("title", startLabel);
    // Restore stop icon only once the tournament has actually transitioned
    // away from running — otherwise mid-flight games-count refreshes would
    // clear the spinner before the row visually reflects the paused state.
    if (!ribbonStopBtn.querySelector("wa-icon") && status !== "running") {
      ribbonStopBtn.innerHTML = '<wa-icon name="pause"></wa-icon>';
    }
  }

  listEl.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp" && ev.key !== "Home" && ev.key !== "End") return;
    const sorted = sortedTournaments();
    if (sorted.length === 0) return;
    const cur = sorted.findIndex((t) => t.id === selectedId);
    let next = cur;
    if (ev.key === "ArrowDown") next = cur < 0 ? 0 : Math.min(cur + 1, sorted.length - 1);
    else if (ev.key === "ArrowUp") next = cur < 0 ? sorted.length - 1 : Math.max(cur - 1, 0);
    else if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = sorted.length - 1;
    if (next === cur) { ev.preventDefault(); return; }
    ev.preventDefault();
    selectedId = sorted[next].id;
    for (const el of listEl.querySelectorAll(".tournament-row.selected")) el.classList.remove("selected");
    const li = listEl.querySelector(`.tournament-row[data-id="${selectedId}"]`);
    if (li) {
      li.classList.add("selected");
      li.scrollIntoView({ block: "nearest" });
    }
    syncRibbon();
  });

  ribbonStartBtn.addEventListener("click", () => {
    const t = selectedTournament();
    if (t && !ribbonStartBtn.disabled) startOne(t);
  });
  ribbonStopBtn.addEventListener("click", async () => {
    const t = selectedTournament();
    if (!t || ribbonStopBtn.disabled) return;
    ribbonStopBtn.disabled = true;
    ribbonStopBtn.innerHTML = '<wa-spinner class="spinner-accent"></wa-spinner>';
    await stopOne(t);
  });
  ribbonWorkspaceBtn.addEventListener("click", () => {
    const t = selectedTournament();
    if (t) openWorkspace(t);
  });
  ribbonInfoBtn.addEventListener("click", () => {
    const t = selectedTournament();
    if (t) openInfoDialog(t);
  });
  ribbonRemoveBtn.addEventListener("click", () => {
    const t = selectedTournament();
    if (t && !ribbonRemoveBtn.disabled) removeOne(t);
  });

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
    const ribbon = container.querySelector(".tournaments-ribbon");
    const rect = menubar.getBoundingClientRect();
    const ribbonRect = ribbon ? ribbon.getBoundingClientRect() : null;
    const ribbonRight = (ribbonRect && ribbonRect.left < 8) ? Math.round(ribbonRect.right) : 0;
    const top = Math.round(rect.bottom);
    const left = Math.max(Math.round(rect.left), ribbonRight);
    openTournamentWorkspace({ api, events, log, token, tournament: t, top, left });
    syncWindowMenu();
  }

  // ---- Info dialog -------------------------------------------------------

  async function openInfoDialog(t) {
    let detailed = t;
    try {
      detailed = await api("GET", `/api/tournaments/${t.id}`);
    } catch (e) {
      log?.("Loading tournament details failed:", e);
    }
    showDialog({
      label: detailed.name,
      width: "520px",
      body: (resolve, dialog) => {
        const wrap = document.createElement("div");
        wrap.className = "tournament-info";
        wrap.appendChild(buildInfoContent(detailed));
        dialog.appendChild(wrap);
      },
    });
  }

  function totalGames(t) {
    const tpl = t.template || {};
    const n = (t.engines || []).length;
    const rounds = Number(tpl.rounds);
    const gpr = Number(tpl.games_per_round ?? 2);
    if (!n || !rounds || !gpr) return null;
    if (tpl.tournament_type === "gauntlet") {
      const seeds = Number(tpl.seeds);
      if (!seeds || seeds >= n) return null;
      return seeds * (n - seeds) * rounds * gpr;
    }
    const pairings = (n * (n - 1)) / 2;
    return pairings * rounds * gpr;
  }

  function formatGames(t) {
    const played = t.standings?.games;
    const total = totalGames(t);
    if (played == null && total == null) return null;
    if (total == null) return String(played ?? 0);
    return `${played ?? 0} of ${total}`;
  }

  function buildInfoContent(t) {
    const tpl = t.template || {};
    const dl = document.createElement("dl");
    dl.className = "tournament-info-grid";

    const row = (label, value) => {
      if (value == null || value === "") return;
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      if (value instanceof Node) dd.appendChild(value);
      else dd.textContent = String(value);
      dl.append(dt, dd);
    };

    row("ID", t.id);
    row("Status", t.status === "stopped" ? "paused" : t.status);
    row("Type", formatType(tpl.tournament_type));
    row("Time control", tpl.tc);
    row("Rounds", tpl.rounds);
    row("Parallel games", tpl.games_in_parallel);
    row("Games", formatGames(t));
    if (tpl.tournament_type === "gauntlet") row("Seeds", tpl.seeds);
    row("Ponder", tpl.ponder ? "On" : "Off");
    row("Resign", formatResign(tpl.resign));
    row("Draw adjudication", formatDraw(tpl.draw));
    const ed = t.engine_defaults || {};
    row("Threads", ed.threads);
    row("Hash (MB)", ed.hash_mb);
    row("Syzygy", ed.syzygy_path);
    if (ed.book_path) {
      const span = document.createElement("span");
      span.textContent = basename(ed.book_path);
      span.title = ed.book_path;
      row("Opening book", span);
    }
    row("Book plies", ed.book_plies);
    row("Book order", ed.book_order);
    row("Created", formatTime(t.created_at));
    row("Started", formatTime(t.started_at));
    row("Stopped", formatTime(t.stopped_at));

    const enginesList = document.createElement("ul");
    enginesList.className = "tournament-info-engines";
    for (const e of t.engines || []) {
      const li = document.createElement("li");
      li.textContent = e.name + (e.version ? ` (${e.version})` : "");
      enginesList.appendChild(li);
    }
    if (enginesList.children.length) row("Engines", enginesList);

    return dl;
  }

  function basename(p) {
    if (!p) return p;
    return p.split(/[\\/]/).pop() || p;
  }

  function formatType(v) {
    if (!v) return null;
    return v === "roundrobin" ? "Round-robin" : v.charAt(0).toUpperCase() + v.slice(1);
  }
  function formatResign(r) {
    if (!r || r.movecount == null || r.score == null) return "Off";
    return `after ${r.movecount} moves at ±${r.score} cp`;
  }
  function formatDraw(d) {
    if (!d || d.movenumber == null) return "Off";
    return `from move ${d.movenumber}, ${d.movecount} moves within ±${d.score} cp`;
  }
  function formatTime(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString();
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

  function syncSortMenu() {
    for (const opt of container.querySelectorAll(".tmb-sort-opt")) {
      opt.classList.toggle("is-active", opt.dataset.sort === sortBy);
    }
  }
  syncSortMenu();

  sortMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = sortMenu.classList.contains("open");
    closeMenus();
    if (!isOpen) sortMenu.classList.add("open");
  });
  for (const opt of container.querySelectorAll(".tmb-sort-opt")) {
    opt.addEventListener("click", () => {
      const next = opt.dataset.sort;
      if (VALID_SORTS.has(next) && next !== sortBy) {
        sortBy = next;
        localStorage.setItem(SORT_KEY_LS, sortBy);
        syncSortMenu();
        renderList();
        toast(`Tournaments sorted by ${opt.textContent.trim()}`);
      }
      closeMenus();
    });
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
  container.querySelector(".tmb-minall").addEventListener("click", () => {
    closeMenus();
    getActiveWorkspace()?.minimizeAll();
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
            <div class="nt-engine-builder"></div>
          </div>

          <div class="nt-section">
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
  window.addEventListener("sturddle:workspace-closed", syncWindowMenu);

  // ---- Initial load -------------------------------------------------------

  syncWindowMenu();
  // Visibility is driven by the Engines tab group (see engines.js):
  // the workspace stays hidden unless the Tournaments sub-tab is active.

  (async () => {
    await loadSettings();
    await loadList();
  })();

  return {
    unmount() {
      offEvents();
      window.removeEventListener("sturddle:settings-changed", onSettingsChanged);
      window.removeEventListener("sturddle:workspace-closed", syncWindowMenu);
      document.removeEventListener("click", closeMenus);
      // Hide (don't close) so the workspace survives perspective
      // navigation; it'll be re-shown when the user returns.
      getActiveWorkspace()?.hide();
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
        <div class="ne-pane-title">Engines</div>
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
