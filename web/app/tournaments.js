// Tournaments perspective: master list + "New Tournament" verb.
// Path/defaults configuration lives in the global Settings dialog under
// the "Tournament" tab — not here.
//
// Row-targeted verbs (Start/Resume, Pause, Open workspace, Info, Remove)
// live in a left-side vertical ribbon that mirrors the Play perspective's
// look and feel. Clicking a row selects it; ribbon actions target the
// selected tournament. New / Sort / Window remain in the top menubar.

import { apiErrorDetail, confirm, reportError, showDialog, toast } from "./dialogs.js";
import { openSettingsDialog } from "./settings-dialog.js";
import { EVT, KIND, STATUS } from "./tournament-events.js";
import { mountTournamentTemplateForm } from "./tournament-template-form.js";
import { hasStaleLiveGames } from "./tournament-live-game.js";
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
            <li class="tmb-dd-submenu">
              <button class="tmb-dd-item tmb-sys-trigger">Tournament</button>
              <ul class="tmb-dropdown">
                <li><button class="tmb-dd-item tmb-sys-standings">Standings</button></li>
                <li><button class="tmb-dd-item tmb-sys-schedule">Live Games</button></li>
                <li><button class="tmb-dd-item tmb-sys-engines">Engines</button></li>
                <li><button class="tmb-dd-item tmb-sys-log">Event Log</button></li>
              </ul>
            </li>
            <li class="tmb-separator"></li>
            <li><button class="tmb-dd-item tmb-tile">Tile</button></li>
            <li><button class="tmb-dd-item tmb-cascade">Cascade</button></li>
            <li><button class="tmb-dd-item tmb-hideall">Hide All</button></li>
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
          <button class="ribbon-btn t-edit" disabled aria-label="Edit" title="Edit">
            <wa-icon name="pen-to-square"></wa-icon>
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

  const ribbonStopBtn = container.querySelector(".t-stop");
  const ribbonWorkspaceBtn = container.querySelector(".t-workspace");
  const ribbonInfoBtn = container.querySelector(".t-info");
  const ribbonEditBtn = container.querySelector(".t-edit");
  const ribbonRemoveBtn = container.querySelector(".t-remove");

  const SORT_KEY_LS = "sturddle.tournaments.sortBy";
  const SORT_ASC_LS = "sturddle.tournaments.sortAsc";
  const VALID_SORTS = new Set(["name", "status", "created_at", "started_at"]);
  let sortBy = VALID_SORTS.has(localStorage.getItem(SORT_KEY_LS))
    ? localStorage.getItem(SORT_KEY_LS) : "created_at";
  let sortAsc = localStorage.getItem(SORT_ASC_LS) !== "false";

  let tournaments = [];
  let activeId = null;
  let selectedId = null;
  let stoppingId = null;
  let startingId = null;
  let settings = null; // { fastchess_path, tournaments_root, default_template, fastchess_detected }

  // Wraps an async function so concurrent calls are dropped until it resolves.
  function guard(fn) {
    let inflight = false;
    return async (...args) => {
      if (inflight) return;
      inflight = true;
      try { await fn(...args); } finally { inflight = false; }
    };
  }

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
      emptyMsg.replaceChildren();
      emptyMsg.append("fastchess not configured — open ");
      const link = document.createElement("a");
      link.href = "#";
      link.className = "settings-deeplink";
      link.textContent = "Settings → Tournament";
      link.addEventListener("click", (e) => {
        e.preventDefault();
        openSettingsDialog({ api, initialTab: "tournament" });
      });
      emptyMsg.append(link, " to set the binary path.");
      newBtn.disabled = true;
      selectedId = null;
      syncRibbon();
      return;
    }
    newBtn.disabled = false;

    if (noTournaments) {
      emptyEl.classList.remove("hidden");
      emptyMsg.replaceChildren();
      const newLink = document.createElement("button");
      newLink.type = "button";
      newLink.className = "toast-icon-btn";
      newLink.setAttribute("aria-label", "New tournament");
      newLink.setAttribute("title", "New tournament");
      const newIc = document.createElement("wa-icon");
      newIc.setAttribute("name", "plus");
      newLink.appendChild(newIc);
      newLink.addEventListener("click", () => openNewTournamentDialog());
      emptyMsg.append("No tournaments yet — click ", newLink, " to create one.");
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
    const running = sorted.find((t) => t.status === STATUS.RUNNING);
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
    const dir = sortAsc ? 1 : -1;
    const cmp = (a, b) => {
      const av = a[sortBy] ?? "";
      const bv = b[sortBy] ?? "";
      if (av === bv) return a.created_at.localeCompare(b.created_at);
      // Empty values sink to the bottom regardless of direction.
      if (av === "") return 1;
      if (bv === "") return -1;
      if (sortBy === "name") return dir * av.localeCompare(bv, undefined, { sensitivity: "base" });
      return dir * (av < bv ? -1 : 1);
    };
    return arr.sort(cmp);
  }

  function renderRow(t) {
    const li = document.createElement("li");
    li.className = "tournament-row" + (t.id === selectedId ? " selected" : "");
    li.dataset.id = t.id;

    const status = t.status;
    const isRunning = status === STATUS.RUNNING;
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
        <span class="tournament-status status-${status}">${status === STATUS.STOPPED ? "paused" : status}</span>
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
      navigateTo(t.id);
    });
    li.addEventListener("dblclick", () => openInfoGuarded(t));

    return li;
  }

  function selectedTournament() {
    return tournaments.find((t) => t.id === selectedId) || null;
  }

  async function navigateTo(newId) {
    const ws = getActiveWorkspace();
    const hadWorkspace = ws && ws.tournamentId !== newId;
    if (hadWorkspace) {
      if (hasStaleLiveGames()) {
        const ok = await confirm({ message: "Live game windows are open. Close them and change active selection?" });
        if (!ok) return false;
      }
      ws.close();
    }
    selectedId = newId;
    for (const el of listEl.querySelectorAll(".tournament-row.selected")) el.classList.remove("selected");
    const li = listEl.querySelector(`.tournament-row[data-id="${newId}"]`);
    if (li) {
      li.classList.add("selected");
      li.scrollIntoView({ block: "nearest" });
    }
    syncRibbon();
    if (hadWorkspace) {
      const t = selectedTournament();
      if (t) openWorkspace(t);
    }
    return true;
  }

  function syncRibbon() {
    const t = selectedTournament();
    if (!t) {
      ribbonStartBtn.disabled = true;
      ribbonStopBtn.disabled = true;
      ribbonWorkspaceBtn.disabled = true;
      ribbonInfoBtn.disabled = true;
      ribbonEditBtn.disabled = true;
      ribbonRemoveBtn.disabled = true;
      if (!ribbonStartBtn.querySelector("wa-icon")) ribbonStartBtn.innerHTML = '<wa-icon class="t-start-icon" name="play"></wa-icon>';
      else ribbonStartBtn.querySelector("wa-icon").setAttribute("name", "play");
      ribbonStartBtn.setAttribute("aria-label", "Start");
      ribbonStartBtn.setAttribute("title", "Start");
      return;
    }
    const isActive = t.id === activeId;
    const anotherRunning = activeId !== null && !isActive;
    const status = t.status;
    const isResume = status === STATUS.STOPPED || status === STATUS.FAILED;

    // !!startingId: only one tournament may start at a time (by design).
    ribbonStartBtn.disabled = isActive || anotherRunning || status === STATUS.RUNNING || status === STATUS.DONE || !!startingId;
    ribbonStopBtn.disabled = !isActive;
    ribbonRemoveBtn.disabled = isActive;
    ribbonWorkspaceBtn.disabled = !!getActiveWorkspace();
    ribbonInfoBtn.disabled = false;
    ribbonEditBtn.disabled = isActive || status === STATUS.DONE;

    const starting = t.id === startingId;
    if (starting) {
      ribbonStartBtn.innerHTML = '<wa-spinner class="spinner-accent"></wa-spinner>';
    } else {
      if (!ribbonStartBtn.querySelector("wa-icon")) ribbonStartBtn.innerHTML = '<wa-icon class="t-start-icon" name="play"></wa-icon>';
      else ribbonStartBtn.querySelector("wa-icon").setAttribute("name", isResume ? "forward-step" : "play");
    }
    const startLabel = isResume ? "Resume" : "Start";
    ribbonStartBtn.setAttribute("aria-label", startLabel);
    ribbonStartBtn.setAttribute("title", startLabel);
    const stopping = t.id === stoppingId;
    if (stopping) {
      ribbonStopBtn.disabled = true;
      ribbonStopBtn.innerHTML = '<wa-spinner class="spinner-accent"></wa-spinner>';
    } else {
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
    navigateTo(sorted[next].id);
  });

  const removeOneGuarded = guard(removeOne);
  const openInfoGuarded  = guard(openInfoDialog);

  ribbonStartBtn.addEventListener("click", async () => {
    const t = selectedTournament();
    if (!t || ribbonStartBtn.disabled || startingId) return;
    startingId = t.id;
    syncRibbon();
    try { await startOne(t); } finally { startingId = null; syncRibbon(); }
  });
  ribbonStopBtn.addEventListener("click", async () => {
    const t = selectedTournament();
    if (!t || ribbonStopBtn.disabled || stoppingId) return;
    stoppingId = t.id;
    syncRibbon();
    try { await stopOne(t); } finally { stoppingId = null; syncRibbon(); }
  });
  ribbonWorkspaceBtn.addEventListener("click", () => {
    const t = selectedTournament();
    if (t) openWorkspace(t);
  });
  ribbonInfoBtn.addEventListener("click", () => {
    const t = selectedTournament();
    if (t) openInfoGuarded(t);
  });
  ribbonEditBtn.addEventListener("click", () => {
    const t = selectedTournament();
    if (t && !ribbonEditBtn.disabled) openEditTournamentDialog(t);
  });
  ribbonRemoveBtn.addEventListener("click", () => {
    const t = selectedTournament();
    if (t && !ribbonRemoveBtn.disabled) removeOneGuarded(t);
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
      message: `Remove "${t.name}"? This deletes its entire directory and games.`,
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
    syncRibbon();
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

    row("ID", makeIdCell(t.id));
    row("Status", t.status === STATUS.STOPPED ? "paused" : t.status);
    if (t.last_error) {
      const tail = (t.last_error.stderr_tail || []).slice(-10).join("\n");
      const pre = document.createElement("pre");
      pre.className = "tournament-info-error";
      pre.textContent = tail || `exit code ${t.last_error.rc}`;
      row(`Last error (rc=${t.last_error.rc})`, pre);
    }
    row("Type", formatType(tpl.tournament_type));
    row("Time control", tpl.tc);
    row("Rounds", tpl.rounds);
    row("Parallel games", tpl.games_in_parallel);
    row("Games", formatGames(t));
    if (tpl.tournament_type === "gauntlet") row("Seeds", tpl.seeds);
    row("Ponder", tpl.ponder ? "On" : "Off");
    row("CPU affinity", tpl.pin_affinity ? "Pinned" : "Off");
    row("Resign", formatResign(tpl.resign));
    row("Draw adjudication", formatDraw(tpl.draw));
    const ed = t.engine_defaults || {};
    row("Threads", ed.threads);
    row("Hash (MB)", ed.hash_mb);
    if (ed.syzygy_path) {
      const span = document.createElement("span");
      span.textContent = basename(ed.syzygy_path);
      span.title = ed.syzygy_path;
      row("Syzygy", span);
    }
    if (ed.book_path) {
      const span = document.createElement("span");
      span.textContent = basename(ed.book_path);
      span.title = ed.book_path;
      row("Opening book", span);
    }
    row("Book plies", ed.book_plies);
    row("Book order", ed.book_order);
    row("Created", formatTime(t.created_at));
    if (t.status === STATUS.RUNNING || t.status === STATUS.FAILED || t.status === STATUS.STOPPED) row("Started", formatTime(t.started_at));
    if (t.status === STATUS.STOPPED) row("Paused", formatTime(t.stopped_at));

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

  const ID_ELLIPSIS = "…";
  function makeIdCell(id) {
    if (!id) return id;
    const s = String(id);
    const span = document.createElement("span");
    span.className = "tournament-id";
    span.title = s;
    span.textContent = s;
    const fit = () => fitMiddleEllipsis(span, s);
    requestAnimationFrame(fit);
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(fit).observe(span);
    }
    return span;
  }

  function fitMiddleEllipsis(el, full) {
    el.textContent = full;
    if (el.scrollWidth <= el.clientWidth) return;
    let lo = 1, hi = full.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      const head = Math.ceil(mid / 2);
      const tail = mid - head;
      el.textContent = full.slice(0, head) + ID_ELLIPSIS + full.slice(full.length - tail);
      if (el.scrollWidth <= el.clientWidth) lo = mid;
      else hi = mid - 1;
    }
    const head = Math.ceil(lo / 2);
    const tail = lo - head;
    el.textContent = full.slice(0, head) + ID_ELLIPSIS + full.slice(full.length - tail);
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

  // ---- New / Edit Tournament dialogs -------------------------------------

  newBtn.addEventListener("click", () => openNewTournamentDialog());

  // Shared dialog body for both create and edit flows.
  // Returns a Promise that resolves to {name, template, engines} or null.
  async function openTournamentDialog({ label, actionLabel, initialName, initialEngines, initialTemplate, available }) {
    const defaults =
      initialTemplate ||
      (settings && settings.default_template) ||
      { tc: "10+0.1", rounds: 10, games_in_parallel: 1 };

    return showDialog({
      label,
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

        const nameInput = wrap.querySelector(".nt-name");
        nameInput.value = initialName || "";

        const builderHost = wrap.querySelector(".nt-engine-builder");
        const builder = mountEngineBuilder({
          host: builderHost,
          available,
          initial: initialEngines || [],
        });

        const formHost = wrap.querySelector(".nt-template-host");
        const tplCtl = mountTournamentTemplateForm({
          container: formHost,
          initialValues: defaults,
        });

        const actionBtn = document.createElement("wa-button");
        actionBtn.slot = "footer";
        actionBtn.size = "small";
        actionBtn.variant = "brand";
        actionBtn.textContent = actionLabel;

        function isValid() {
          return (nameInput.value || "").trim() !== "" && builder.getEngines().length >= 2;
        }

        function refreshValidity() {
          actionBtn.disabled = !isValid();
        }
        refreshValidity();
        nameInput.addEventListener("input", refreshValidity);
        builder.onChange(refreshValidity);

        actionBtn.addEventListener("click", async () => {
          if (!isValid()) return;
          const v = tplCtl.validate({ numEngines: builder.getEngines().length });
          if (!v.ok) {
            toast(v.errors[0].message, { variant: "danger", duration: 6000 });
            return;
          }
          let template;
          try {
            template = tplCtl.getValues();
          } catch (e) {
            toast(e.message, { variant: "danger" });
            return;
          }

          const picked = builder.getPickedRegistry();
          const globalDefaults = await loadGlobalEngineDefaults();
          const resolved = resolveResourceParams(template, picked, globalDefaults);
          actionBtn.loading = true;
          let rescheckResult;
          try {
            rescheckResult = await api("POST", "/api/tournaments/rescheck", resolved);
          } catch (e) {
            const detail = apiErrorDetail(e);
            const msg = (detail && detail.message) || detail || "Resource check failed";
            toast(typeof msg === "string" ? msg : String(msg), {
              variant: "danger", duration: 8000,
            });
            actionBtn.loading = false;
            return;
          } finally {
            actionBtn.loading = false;
          }
          if (rescheckResult.warnings && rescheckResult.warnings.length) {
            for (const w of rescheckResult.warnings) {
              toast(`Warning: ${w.message}`, { variant: "warning", duration: 8000 });
            }
          }

          template.max_threads = resolved.max_threads;
          template.max_hash_mb = resolved.max_hash_mb;

          resolve({
            name: nameInput.value.trim(),
            template,
            engines: builder.getEngines(),
          });
        });

        dialog.append(wrap, actionBtn);
        requestAnimationFrame(() => nameInput.focus());
      },
    });
  }

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

    const result = await openTournamentDialog({
      label: "New Tournament",
      actionLabel: "Create",
      initialName: "",
      initialEngines: [],
      initialTemplate: (settings && settings.default_template) || null,
      available,
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

  async function openEditTournamentDialog(t) {
    // PRE-OPEN gate: warn early so the user can bail without loading the
    // registry or filling the dialog. Keep this even though there is also a
    // post-dialog confirm — the two guards serve different purposes: this one
    // is a cheap "heads up" before any work; the post-dialog one fires only
    // when the engine roster actually changed and gives a last chance to abort.
    const hasGames = (t.standings?.games ?? 0) > 0;
    if (hasGames) {
      const proceed = await confirm({
        message: `"${t.name}" has recorded games. Editing will delete all game results. Continue?`,
        okLabel: "Continue",
        destructive: true,
      });
      if (!proceed) return;
    }

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

    // Resolve the tournament's current engines to registry entries so the
    // builder can preselect them. Prefer name match (canonical key for the
    // registry); fall back to cmd so a renamed entry still preselects.
    const byName = new Map(available.map((e) => [e.name, e]));
    const byCmd = new Map(available.map((e) => [e.cmd, e]));
    const original = t.engines || [];
    const initialEngines = [];
    let droppedCount = 0;
    for (const e of original) {
      const match = byName.get(e.name) || byCmd.get(e.cmd);
      if (match) initialEngines.push(match);
      else droppedCount += 1;
    }
    if (droppedCount > 0) {
      toast(
        `${droppedCount} engine${droppedCount === 1 ? "" : "s"} no longer in the registry — re-add before applying.`,
        { variant: "warning", duration: 8000 },
      );
    }

    const result = await openTournamentDialog({
      label: `Edit "${t.name}"`,
      actionLabel: "Apply",
      initialName: t.name,
      initialEngines,
      initialTemplate: t.template || null,
      available,
    });
    if (!result) return;

    // POST-DIALOG gate: last chance to abort before the destructive PATCH.
    // Any edit (template, engines, or rename) wipes the PGN server-side
    // because past games were played under potentially different conditions
    // and must not mix with future games — so this fires on hasGames alone.
    if (hasGames) {
      const ok = await confirm({
        message: `Applying changes to "${t.name}" will permanently delete its recorded games. This cannot be undone.`,
        okLabel: "Apply & Delete Games",
        destructive: true,
      });
      if (!ok) return;
    }

    try {
      await api("PATCH", `/api/tournaments/${t.id}`, result);
      toast(`Updated "${result.name}"`, { variant: "neutral" });
    } catch (e) {
      reportError({ log }, "Updating tournament failed", e);
    }
    // Refresh either way: success applied changes; failure may indicate the
    // local view drifted (e.g. tournament started elsewhere) and should
    // re-sync.
    await loadList();
  }

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
    for (const opt of container.querySelectorAll(".tmb-sort-opt")) {
      const active = opt.dataset.sort === sortBy;
      if (active) opt.dataset.dir = sortAsc ? "asc" : "desc";
      else delete opt.dataset.dir;
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
      if (!VALID_SORTS.has(next)) { closeMenus(); return; }
      const dir = () => sortAsc ? "ascending" : "descending";
      if (next === sortBy) {
        sortAsc = !sortAsc;
        localStorage.setItem(SORT_ASC_LS, String(sortAsc));
        toast(`Sorted by ${opt.textContent.trim()}, ${dir()}`);
      } else {
        sortBy = next;
        localStorage.setItem(SORT_KEY_LS, sortBy);
        toast(`Sorted by ${opt.textContent.trim()}, ${dir()}`);
      }
      syncSortMenu();
      renderList();
      closeMenus();
    });
  }

  const hideAllBtn = container.querySelector(".tmb-hideall");
  const hiddenDisabledBtns = [".tmb-sys-trigger", ".tmb-tile", ".tmb-cascade"]
    .map(s => container.querySelector(s));

  windowMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (windowMenuBtn.disabled) return;
    const isOpen = windowMenu.classList.contains("open");
    closeMenus();
    if (!isOpen) {
      const ws = getActiveWorkspace();
      const hidden = ws?.isHidden() ?? false;
      hideAllBtn.textContent = hidden ? "Show All" : "Hide All";
      for (const btn of hiddenDisabledBtns) btn.disabled = hidden;
      windowMenu.classList.add("open");
    }
  });

  container.querySelector(".tmb-tile").addEventListener("click", () => {
    closeMenus();
    getActiveWorkspace()?.tile();
  });
  container.querySelector(".tmb-cascade").addEventListener("click", () => {
    closeMenus();
    getActiveWorkspace()?.cascade();
  });
  hideAllBtn.addEventListener("click", () => {
    closeMenus();
    const ws = getActiveWorkspace();
    if (!ws) return;
    ws.isHidden() ? ws.show() : ws.hide();
  });
  container.querySelector(".tmb-closeall").addEventListener("click", () => {
    closeMenus();
    getActiveWorkspace()?.closeAll();
    syncWindowMenu();
  });
  for (const [cls, key] of [
    [".tmb-sys-standings", "standings"],
    [".tmb-sys-schedule",  "schedule"],
    [".tmb-sys-engines",   "engines"],
    [".tmb-sys-log",       "log"],
  ]) {
    container.querySelector(cls).addEventListener("click", () => {
      closeMenus();
      getActiveWorkspace()?.openSystemWindow(key);
    });
  }

  document.addEventListener("click", closeMenus);

  async function loadGlobalEngineDefaults() {
    try {
      const s = await api("GET", "/settings");
      return {
        threads: s.engine_default_threads,
        hash_mb: s.engine_default_hash_mb,
      };
    } catch {
      return { threads: null, hash_mb: null };
    }
  }

  // Resolve worst-case threading + hash from the picked engines and the
  // global engine_default_* override. Mirrors the formula in
  // docs/tournament-concurrency-plan.md so the rescheck endpoint sees
  // the same numbers the user is committing to.
  function resolveResourceParams(template, pickedRegistry, globalDefaults) {
    function resolvedFor(engine, optName, fallback) {
      const opt = engine.options && engine.options[optName];
      if (opt != null && opt !== "") return Number(opt);
      const schema = engine.option_schema && engine.option_schema[optName];
      if (schema && schema.default != null) return Number(schema.default);
      return fallback;
    }
    const maxOver = (key, fallback) => {
      if (!pickedRegistry.length) return fallback;
      return pickedRegistry.reduce(
        (acc, e) => Math.max(acc, resolvedFor(e, key, fallback)),
        0,
      ) || fallback;
    };
    const max_threads = globalDefaults.threads
      ? Number(globalDefaults.threads)
      : maxOver("Threads", 1);
    const max_hash_mb = globalDefaults.hash_mb
      ? Number(globalDefaults.hash_mb)
      : maxOver("Hash", 16);

    return {
      parallel: Number(template.games_in_parallel || 1),
      max_threads,
      max_hash_mb,
      ponder: !!template.ponder,
      pin_affinity: !!template.pin_affinity,
      allow_oversubscribe: !!template.allow_oversubscribe,
    };
  }

  // ---- Live updates from WS ----------------------------------------------

  const offEvents = events.on((evt) => {
    if (evt.kind === EVT.STATUS || evt.kind === EVT.UPDATE) {
      // Surface runner crashes as a toast — the user may not have a
      // workspace open and would otherwise see the row silently flip
      // to a terminal state with no explanation.
      const inner = evt.payload?.kind;
      if (inner === KIND.RUNNER_CRASH) {
        const tid = evt.payload?.tournament_id;
        const t = tournaments.find((x) => x.id === tid);
        const name = t ? t.name : "Tournament";
        const tail = evt.payload?.stderr_tail || [];
        const firstErr = tail.find((l) => /error|fatal|fail/i.test(l)) || tail[0] || `exit code ${evt.payload?.rc}`;
        toast(`${name} failed: ${firstErr}`, { variant: "danger", duration: 10000 });
      }
      loadList();
    }
  });

  // Settings can change in another tab/dialog — pick those up too.
  function onSettingsChanged() {
    loadSettings();
  }
  window.addEventListener("sturddle:settings-changed", onSettingsChanged);
  window.addEventListener("sturddle:workspace-closed", () => { syncWindowMenu(); syncRibbon(); });

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
      li.title = e.name;
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
    getPickedRegistry() {
      // Full registry entries (with options + option_schema) for the
      // picked engines — used by the rescheck resolver.
      return pickedIds.map((id) => byId.get(id)).filter(Boolean);
    },
    onChange(fn) { listeners.add(fn); },
  };
}
