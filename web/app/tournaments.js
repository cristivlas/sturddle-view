// Tournaments perspective: master list + "New Tournament" verb.
// Path/defaults configuration lives in the global Settings dialog under
// the "Tournament" tab — not here.
//
// Row-targeted verbs (Start/Restart, Stop, Open workspace, Info, Remove)
// live in a left-side vertical ribbon that mirrors the Play perspective's
// look and feel. Clicking a row selects it; ribbon actions target the
// selected tournament. New / Sort / Window remain in the top menubar.

import { mqMobile, mqMobileH } from "./breakpoints.js";
import { apiErrorDetail, buildToastWithActions, confirm, makeToastDismissBtn, OPEN_ENGINES_ACTION, reportError, showDialog, toast } from "./dialogs.js";
import { openSettingsDialog } from "./settings-dialog.js";
import { EVT, KIND, POLL_INTERVAL_MS, STATUS } from "./tournament-events.js";
import { CONFIRM_WIPE_QS, buildRestartConfirm } from "./tournament-restart.js";
import { mountTournamentTemplateForm } from "./tournament-template-form.js";
import { clearWorkspaceState, getActiveLayout, getActiveWorkspace, hasSavedWorkspaceState, LAYOUT, openTournamentWorkspace } from "./tournament-workspace.js";

const NEED_TWO_ENGINES_MSG = "Register at least 2 engines first.";
// Fallback ribbon width when the docked ribbon is unavailable (floating mode);
// matches the `--ribbon-w` CSS var on .tournaments-body.
const RIBBON_W_FALLBACK_PX = 36;

export function mountTournaments({ container, api, events, log, token }) {
  container.innerHTML = `
    <div class="tournaments-panel">
      <menu class="tournaments-menubar">
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
                <li class="tmb-separator"></li>
                <li><button class="tmb-dd-item tmb-sys-log">Event Log</button></li>
              </ul>
            </li>
            <li class="tmb-separator"></li>
            <li><button class="tmb-dd-item tmb-snap">Snap</button></li>
            <li><button class="tmb-dd-item tmb-tile">Tile</button></li>
            <li><button class="tmb-dd-item tmb-tidy">Clean</button></li>
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
          <button class="ribbon-btn t-stop" disabled aria-label="Stop" title="Stop">
            <wa-icon name="hand"></wa-icon>
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

  const SORT_KEY_LS = "sturddle:tournaments:sortBy";
  const SORT_ASC_LS = "sturddle:tournaments:sortAsc";
  const VALID_SORTS = new Set(["name", "status", "created_at", "started_at"]);
  let sortBy = VALID_SORTS.has(localStorage.getItem(SORT_KEY_LS))
    ? localStorage.getItem(SORT_KEY_LS) : "created_at";
  let sortAsc = localStorage.getItem(SORT_ASC_LS) !== "false";

  let tournaments = [];
  let activeId = null;
  let selectedId = null;
  let initialLoad = true;
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

  function debounce(fn, ms) {
    let timer = null;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  }

  // Returns an async function that drops its result if a newer call
  // has been initiated. Always resolves with undefined --
  // await is fire-and-forget; do not read state immediately after.
  // `fetch` returns data; `commit` writes it; `onError` handles fetch errors.
  function lastWriteWins(fetch, commit, onError) {
    let gen = 0;
    return async (...args) => {
      const v = ++gen;
      let data;
      try { data = await fetch(...args); } catch (e) { onError(e); return; }
      if (v !== gen) return;
      commit(data);
    };
  }

  // ---- API helpers --------------------------------------------------------

  const loadSettings = lastWriteWins(
    () => api("GET", "/api/tournament-settings"),
    (data) => { settings = data; renderList(); },
    (e) => reportError({ log }, "Loading tournament settings failed", e),
  );

  const loadList = lastWriteWins(
    () => api("GET", "/api/tournaments"),
    (body) => {
      tournaments = body.tournaments;
      activeId = body.active_id;
      renderList();
      syncWorkspaceOtherActive();
    },
    (e) => reportError({ log }, "Loading tournaments failed", e),
  );

  function syncWorkspaceOtherActive() {
    const ws = getActiveWorkspace();
    if (!ws?.setOtherActive) return;
    if (!activeId || activeId === ws.tournamentId) {
      ws.setOtherActive(null, null);
      return;
    }
    const other = tournaments.find((x) => x.id === activeId);
    ws.setOtherActive(activeId, other?.name || null);
  }
  const debouncedLoadList = debounce(loadList, 150);

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
    syncRibbon();
    if (initialLoad) {
      initialLoad = false;
      maybeRestoreWorkspace();
    }
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
        </div>
        <span class="tournament-progress-label">${played} / ${total} · ${pct}%</span>
      `;
    } else {
      trailing = `<span class="tournament-engines muted"></span>`;
    }

    const sprtBadge = t.template?.sprt ? `<span class="tournament-sprt-badge">SPRT</span>` : "";
    li.innerHTML = `
      <div class="tournament-row-main">
        <span class="tournament-status status-${status}">${status}</span>
        <span class="tournament-name"></span>
        ${sprtBadge}
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

  function updateProgressInPlace(t) {
    const played = t?.standings?.games;
    if (played == null) return;
    const row = listEl.querySelector(`li[data-id="${t.id}"]`);
    if (!row) return;
    const bar = row.querySelector(".tournament-progress");
    const fill = row.querySelector(".tournament-progress-fill");
    const label = row.querySelector(".tournament-progress-label");
    if (!bar || !fill || !label) return;
    const total = totalGames(t);
    if (!total) return;
    const pct = Math.min(100, Math.round((played / total) * 100));
    bar.setAttribute("aria-valuenow", String(played));
    fill.style.width = `${pct}%`;
    label.textContent = `${played} / ${total} · ${pct}%`;
  }

  function selectedTournament() {
    return tournaments.find((t) => t.id === selectedId) || null;
  }

  function dismissSortToastNow() {
    dismissSortToast?.();
    dismissSortToast = null;
    sortToastTextEl = null;
    sortToastToggleBtn = null;
    sortToastHiddenWbs = [];
  }

  function teardownWorkspace(ws) {
    dismissSortToastNow();
    ws.close();
    // Note: if the user closes all windows individually, finalize() fires
    // inside tournament-workspace.js with no callback here, so the sort
    // toast may linger with stale WinBox refs. Harmless (restoreWindows
    // swallows errors), but not covered by this fix.
  }

  async function navigateTo(newId) {
    const ws = getActiveWorkspace();
    const hadWorkspace = ws && ws.tournamentId !== newId;
    if (hadWorkspace) {
      // Live game windows close here but are restored when switching back
      // (if the tournament is still running), so no confirmation needed.
      teardownWorkspace(ws);
    }
    selectedId = newId;
    for (const el of listEl.querySelectorAll(".tournament-row.selected")) el.classList.remove("selected");
    const li = listEl.querySelector(`.tournament-row[data-id="${newId}"]`);
    if (li) {
      li.classList.add("selected");
      li.scrollIntoView({ block: "nearest" });
    }
    syncRibbon();
    const t = selectedTournament();
    if (t && hasSavedWorkspaceState(t.id)) {
      // Only auto-open when the tournament has saved workspace state with
      // open windows. Brand-new or explicitly-dismissed tournaments stay
      // closed -- the user can open them manually.
      openWorkspace(t);
    }
    // Invariant: an open workspace always reflects the selected tournament.
    // The list-level poll relies on this to drive workspace.refresh() from
    // selectedId without having to track the workspace's pinned tid.
    const wsAfter = getActiveWorkspace();
    if (wsAfter && wsAfter.tournamentId !== selectedId) {
      throw new Error(`workspace tid ${wsAfter.tournamentId} != selectedId ${selectedId}`);
    }
    return true;
  }

  function syncRibbon() {
    syncTidyBtn();
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
    // Stopped/failed -> Start = restart from scratch (Stop is destructive;
    // fastchess's resume contract is fragile across stop/resume cycles).
    const isRestart = status === STATUS.STOPPED || status === STATUS.FAILED;

    // !!startingId: only one tournament may start at a time (by design).
    ribbonStartBtn.disabled = isActive || anotherRunning || status === STATUS.RUNNING || status === STATUS.DONE || !!startingId;
    ribbonStopBtn.disabled = !isActive;
    ribbonRemoveBtn.disabled = isActive;
    ribbonWorkspaceBtn.disabled = !!getActiveWorkspace();
    ribbonInfoBtn.disabled = false;
    ribbonEditBtn.disabled = isActive || status === STATUS.DONE;

    const starting = t.id === startingId;
    const startIconName = isRestart ? "rotate-right" : "play";
    if (starting) {
      ribbonStartBtn.innerHTML = '<wa-spinner class="spinner-accent"></wa-spinner>';
    } else if (!ribbonStartBtn.querySelector("wa-icon")) {
      ribbonStartBtn.innerHTML = `<wa-icon class="t-start-icon" name="${startIconName}"></wa-icon>`;
    } else {
      ribbonStartBtn.querySelector("wa-icon").setAttribute("name", startIconName);
    }
    const startLabel = isRestart ? "Restart" : "Start";
    ribbonStartBtn.setAttribute("aria-label", startLabel);
    ribbonStartBtn.setAttribute("title", startLabel);
    const stopping = t.id === stoppingId;
    if (stopping) {
      ribbonStopBtn.disabled = true;
      ribbonStopBtn.innerHTML = '<wa-spinner class="spinner-accent"></wa-spinner>';
    } else {
      ribbonStopBtn.innerHTML = '<wa-icon name="hand"></wa-icon>';
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
    // Stopped/failed tournaments restart from scratch: wipe the dir then
    // launch fresh. Confirm before destroying games.
    const willWipe = t.status === STATUS.STOPPED || t.status === STATUS.FAILED;
    let qs = "";
    if (willWipe) {
      const ok = await confirm(buildRestartConfirm(t.name, t.standings?.games ?? 0));
      if (!ok) return;
      qs = `?${CONFIRM_WIPE_QS}`;
    }
    try {
      await api("POST", `/api/tournaments/${t.id}/start${qs}`);
    } catch (e) {
      reportError({ log }, `Starting "${t.name}" failed`, e);
      return;
    }
    await loadList();
  }

  async function stopOne(t) {
    // Stop is destructive -- on next Start the tournament is restarted
    // from scratch. Warn before clicking through.
    const games = t.standings?.games ?? 0;
    const message = games > 0
      ? `Stop "${t.name}"?\nRestarting discards all ${games} recorded games.`
      : `Stop "${t.name}"?\nOn restart this tournament will start from scratch.`;
    const ok = await confirm({
      message,
      okLabel: "Stop",
      destructive: true,
      messageClass: "confirm-message--multiline",
    });
    if (!ok) return;
    try {
      await api("POST", `/api/tournaments/${t.id}/stop`);
    } catch (e) {
      reportError({ log }, `Stopping "${t.name}" failed`, e);
    }
    await loadList();
  }

  async function removeOne(t) {
    const ok = await confirm({
      message: `Remove "${t.name}"? All games and data will be permanently deleted.`,
      okLabel: "Remove",
      destructive: true,
    });
    if (!ok) return;
    try {
      await api("DELETE", `/api/tournaments/${t.id}`);
      if (getActiveWorkspace()?.tournamentId === t.id) getActiveWorkspace().close();
      clearWorkspaceState(t.id);
      toast(`Removed tournament "${t.name}"`, { variant: "success" });
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
    // Reserve the ribbon's width on BOTH edges regardless of which side
    // it docks to (or whether it's floating). Keeps the workspace symmetric
    // and ribbon-side-flips don't reshape the available area.
    let ribbonW = ribbonRect ? Math.round(ribbonRect.width) : 0;
    if (ribbonW === 0) {
      // Floating: ribbon is detached. Fall back to the --ribbon-w CSS var,
      // then to the hardcoded default if the var is unavailable.
      const body = container.querySelector(".tournaments-body");
      const v = body && parseInt(getComputedStyle(body).getPropertyValue("--ribbon-w"));
      ribbonW = v > 0 ? v : RIBBON_W_FALLBACK_PX;
    }
    const top = Math.round(rect.bottom);
    const left = Math.max(Math.round(rect.left), ribbonW);
    const getRight = () => window.innerWidth - ribbonW;
    openTournamentWorkspace({ api, events, log, token, tournament: t, top, left, getRight });
    // Seed the workspace's view of the other-active tournament so the
    // banner Restart button reflects busy state on open, not just after
    // the next loadList tick.
    syncWorkspaceOtherActive();
    syncWindowMenu();
    syncRibbon();
  }

  // ---- Info dialog -------------------------------------------------------

  const IS_LOCAL = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);

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
      height: "min(720px, 92vh)",
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

    const idCell = document.createElement("div");
    idCell.className = "tournament-id-row";
    const idSpan = makeIdCell(t.id);
    idCell.appendChild(idSpan);
    if (settings?.tournaments_root) {
      const folder = settings.tournaments_root.replace(/[\\/]+$/, "") + "/" + t.id;
      if (IS_LOCAL) {
        const btn = document.createElement("button");
        btn.className = "tournament-info-reveal-btn";
        btn.title = folder;
        btn.innerHTML = `<wa-icon name="folder-open"></wa-icon>`;
        btn.addEventListener("click", debounce(async () => {
          try {
            await api("POST", `/api/tournaments/${t.id}/reveal`);
          } catch (e) {
            reportError({ log }, "Could not open folder", e);
          }
        }, 500));
        idCell.appendChild(btn);
      } else {
        idSpan.title = folder;
      }
    }
    row("ID", idCell);
    row("Status", t.status);
    if (t.last_error) {
      const tail = (t.last_error.stderr_tail || []).slice(-10).join("\n");
      const pre = document.createElement("pre");
      pre.className = "tournament-info-error";
      pre.textContent = tail || `exit code ${t.last_error.rc}`;
      row(`Last error (rc=${t.last_error.rc})`, pre);
    }
    row("Type", formatType(tpl.tournament_type));
    row("Time control", tpl.tc);
    if (tpl.sprt) {
      const s = tpl.sprt;
      row("Rounds", "unlimited (SPRT)");
      row("SPRT", `elo0=${s.elo0} elo1=${s.elo1} alpha=${s.alpha} beta=${s.beta} model=${s.model}`);
    } else {
      row("Rounds", tpl.rounds);
    }
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
    if (t.status === STATUS.STOPPED) row("Stopped", formatTime(t.stopped_at));

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
  async function openTournamentDialog({ label, actionLabel, initialName, initialEngines, initialTemplate, available, onSubmit }) {
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
        tplCtl.setSprtAvailable(builder.getEngines().length === 2);
        nameInput.addEventListener("input", refreshValidity);
        builder.onChange(() => {
          tplCtl.setSprtAvailable(builder.getEngines().length === 2);
          refreshValidity();
        });

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

          const data = {
            name: nameInput.value.trim(),
            template,
            engines: builder.getEngines(),
          };
          if (onSubmit) {
            actionBtn.loading = true;
            try {
              const submitted = await onSubmit(data);
              if (submitted !== false) resolve(data);
            } catch (e) {
              if (e.isNameCollision) {
                nameInput.classList.remove("nt-name-error");
                void nameInput.offsetWidth;
                nameInput.classList.add("nt-name-error");
                nameInput.addEventListener("animationend", () => nameInput.classList.remove("nt-name-error"), { once: true });
              }
            } finally {
              actionBtn.loading = false;
            }
          } else {
            resolve(data);
          }
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
      toast(buildToastWithActions(NEED_TWO_ENGINES_MSG, [OPEN_ENGINES_ACTION]), { variant: "danger" });
      return;
    }

    await openTournamentDialog({
      label: "New Tournament",
      actionLabel: "Create",
      initialName: "",
      initialEngines: [],
      initialTemplate: (settings && settings.default_template) || null,
      available,
      onSubmit: async (data) => {
        try {
          await api("POST", "/api/tournaments", data);
          toast(`Created new tournament "${data.name}"`, { variant: "success" });
        } catch (e) {
          reportError({ log }, "Creating tournament failed", e);
          if (/-> 409\b/.test(e.message)) throw Object.assign(e, { isNameCollision: true });
          return false;
        }
        await loadList();
      },
    });
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
      toast(buildToastWithActions(NEED_TWO_ENGINES_MSG, [OPEN_ENGINES_ACTION]), { variant: "danger" });
      return;
    }

    // Resolve the tournament's current engines to registry entries so the
    // builder can preselect them. Prefer id match; fall back to name then cmd.
    const byId   = new Map(available.map((e) => [e.id,   e]));
    const byName = new Map(available.map((e) => [e.name, e]));
    const byCmd  = new Map(available.map((e) => [e.path, e]));
    const original = t.engines || [];
    const initialEngines = [];
    let droppedCount = 0;
    for (const e of original) {
      const match = byId.get(e.id) || byName.get(e.name) || byCmd.get(e.cmd);
      if (match) initialEngines.push(match);
      else droppedCount += 1;
    }
    if (droppedCount > 0) {
      toast(
        `${droppedCount} engine${droppedCount === 1 ? "" : "s"} no longer in the registry — re-add before applying.`,
        { variant: "warning", duration: 8000 },
      );
    }

    await openTournamentDialog({
      label: `Edit "${t.name}"`,
      actionLabel: "Apply",
      initialName: t.name,
      initialEngines,
      initialTemplate: t.template || null,
      available,
      onSubmit: async (data) => {
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
          if (!ok) return false;
        }
        let failed = false;
        try {
          await api("PATCH", `/api/tournaments/${t.id}`, data);
          toast(`Updated "${data.name}"`, { variant: "success" });
        } catch (e) {
          reportError({ log }, "Updating tournament failed", e);
          if (/-> 409\b/.test(e.message)) throw Object.assign(e, { isNameCollision: true });
          failed = true;
        }
        // Refresh either way: success applied changes; failure may indicate the
        // local view drifted (e.g. tournament started elsewhere) and should
        // re-sync.
        await loadList();
        if (failed) return false;
      },
    });
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
  function applySort(nextBy, nextAsc) {
    sortBy = nextBy;
    sortAsc = nextAsc;
    localStorage.setItem(SORT_KEY_LS, sortBy);
    localStorage.setItem(SORT_ASC_LS, String(sortAsc));
    syncSortMenu();
    renderList();
  }
  // Persistent sort toast -- reuse DOM in place to avoid flicker on re-sort.
  let dismissSortToast = null;
  let sortToastTextEl = null;
  let sortToastToggleBtn = null;
  let sortToastHidden = false;
  let sortToastHiddenWbs = [];

  function ensureSortToast(ws) {
    if (dismissSortToast) return;
    const msg = document.createElement("span");
    msg.className = "toast-sort-msg";
    sortToastTextEl = document.createElement("span");
    sortToastToggleBtn = document.createElement("button");
    sortToastToggleBtn.className = "toast-action-btn toast-ws-toggle toast-ws-minimize";
    sortToastHidden = false;
    sortToastHiddenWbs = [];
    sortToastToggleBtn.addEventListener("click", () => {
      if (!sortToastHidden) {
        sortToastHiddenWbs = ws.minimizeAll();
        sortToastToggleBtn.classList.replace("toast-ws-minimize", "toast-ws-restore");
        sortToastHidden = true;
      } else {
        // Restore is the toast's terminal action: once the user has
        // un-minimized the windows they minimized, the toast has served
        // its purpose. Dismissing avoids a stale "sorted by..." linger.
        ws.restoreWindows(sortToastHiddenWbs);
        dismissSortToastNow();
      }
    });
    const closeBtn = makeToastDismissBtn(dismissSortToastNow);
    msg.append(sortToastTextEl, sortToastToggleBtn, closeBtn);
    dismissSortToast = toast(msg, { duration: 0 });
  }

  for (const opt of container.querySelectorAll(".tmb-sort-opt")) {
    opt.addEventListener("click", () => {
      const next = opt.dataset.sort;
      if (!VALID_SORTS.has(next)) { closeMenus(); return; }
      const nextAsc = next === sortBy ? !sortAsc : sortAsc;
      applySort(next, nextAsc);
      const label = `Tournaments sorted by ${opt.textContent.trim()}, ${nextAsc ? "ascending" : "descending"}`;
      const ws = getActiveWorkspace();
      if (ws) {
        ensureSortToast(ws);
        sortToastTextEl.textContent = label;
      } else {
        toast(label);
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

  const snapBtn = container.querySelector(".tmb-snap");
  const tileBtn = container.querySelector(".tmb-tile");
  const tidyBtn = container.querySelector(".tmb-tidy");
  const syncTidyBtn = () => {
    const layout = getActiveLayout();
    snapBtn.classList.toggle("tmb-active", layout === LAYOUT.SNAP);
    tileBtn.classList.toggle("tmb-active", layout === LAYOUT.TILE);
    tidyBtn.classList.toggle("tmb-active", layout === LAYOUT.TIDY);
  };
  snapBtn.addEventListener("click", () => {
    closeMenus();
    const ws = getActiveWorkspace();
    if (!ws) return;
    if (getActiveLayout() === LAYOUT.SNAP) ws.untidy(); else ws.snap();
    syncTidyBtn();
  });
  tileBtn.addEventListener("click", () => {
    closeMenus();
    const ws = getActiveWorkspace();
    if (!ws) return;
    if (getActiveLayout() === LAYOUT.TILE) ws.untidy(); else ws.tile();
    syncTidyBtn();
  });
  tidyBtn.addEventListener("click", () => {
    closeMenus();
    const ws = getActiveWorkspace();
    if (!ws) return;
    if (getActiveLayout() === LAYOUT.TIDY) ws.untidy(); else ws.tidy();
    syncTidyBtn();
  });
  container.querySelector(".tmb-closeall").addEventListener("click", () => {
    closeMenus();
    const ws = getActiveWorkspace();
    if (ws) { dismissSortToastNow(); ws.closeAll(); }
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
  // global engine_default_* override. Must mirror the rescheck endpoint's
  // formula so it sees the same numbers the user is committing to.
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
      // The /start API doesn't return until orchestrator.start completes
      // (which can include a multi-second PGN rewrite); the status event
      // fires earlier. Clear pending flags here, with an optimistic local
      // status update so syncRibbon reflects the transition immediately.
      const tid = evt.payload?.tournament_id;
      const newStatus = evt.payload?.status;
      const t = tid ? tournaments.find((x) => x.id === tid) : null;
      if (t && newStatus) {
        t.status = newStatus;
        if (newStatus === STATUS.RUNNING) activeId = tid;
        else if (activeId === tid) activeId = null;
      }
      if (startingId === tid && newStatus === STATUS.RUNNING) {
        startingId = null;
      }
      if (
        stoppingId === tid &&
        [STATUS.STOPPED, STATUS.DONE, STATUS.FAILED].includes(newStatus)
      ) {
        stoppingId = null;
      }
      // Re-render with the optimistic state; debouncedLoadList canonicalizes.
      renderList();
      syncWorkspaceOtherActive();
      debouncedLoadList();
    }
  });

  // Settings can change in another tab/dialog — pick those up too.
  function onSettingsChanged() {
    loadSettings();
  }
  window.addEventListener("sturddle:settings-changed", onSettingsChanged);
  window.addEventListener("sturddle:workspace-closed", () => { syncWindowMenu(); syncRibbon(); });

  // Single periodic refresh for the selected tournament when it's running.
  // Hits one endpoint per tick and fans out: list progress bar in place,
  // and the workspace (if open) via applyDetail() so it doesn't re-fetch.
  // navigateTo's invariant guarantees workspace.tournamentId === selectedId
  // when a workspace is open, so we can drive both from selectedId alone.
  async function pollTick() {
    const t = selectedTournament();
    if (!t || t.status !== STATUS.RUNNING) return;
    let fresh;
    try {
      fresh = await api("GET", `/api/tournaments/${t.id}`);
    } catch (e) {
      log?.(`tournaments poll failed: ${e.message}`);
      return;
    }
    // Mutate in place so renderList() / sort / etc. see the latest.
    // Narrow copy: list only consumes status + standings; workspace-only
    // fields stay out of tournaments[] to avoid stale-field confusion.
    t.status = fresh.status;
    t.standings = fresh.standings;
    updateProgressInPlace(t);
    const ws = getActiveWorkspace();
    if (ws && ws.tournamentId === t.id) ws.applyDetail(fresh);
  }
  const pollIntervalId = window.setInterval(pollTick, POLL_INTERVAL_MS);

  // ---- Initial load -------------------------------------------------------

  syncWindowMenu();
  // Visibility is driven by the Engines tab group (see engines.js):
  // the workspace stays hidden unless the Tournaments sub-tab is active.

  // Fire-and-forget: lastWriteWins resolves undefined; state is populated
  // asynchronously and rendered via renderList() inside each commit.
  loadSettings();
  // After initial population, fire one immediate pollTick so a workspace
  // revealed on perspective re-mount catches up without waiting a full
  // POLL_INTERVAL_MS. No-op when nothing's running.
  loadList().then(pollTick);

  // true once engines.js confirms the Tournaments tab is active on load.
  let tournamentsTabActive = false;

  // Workspace windows don't fit a mobile viewport in either axis. Width
  // OR height crossing the threshold counts as mobile.
  const isMobileViewport = () => mqMobile.matches || mqMobileH.matches;

  function maybeRestoreWorkspace() {
    if (!tournamentsTabActive || initialLoad) return;
    const t = selectedTournament();
    if (t && hasSavedWorkspaceState(t.id)) openWorkspace(t);
  }

  function restoreWorkspace() {
    tournamentsTabActive = true;
    // Defer one frame so the tab panel is laid out before openWorkspace
    // measures ribbon/menubar geometry via getBoundingClientRect().
    requestAnimationFrame(maybeRestoreWorkspace);
  }

  // Mobile viewport closes the workspace. No auto-reopen on widen --
  // user must manually reopen via the ribbon button.
  const onViewportChange = () => {
    if (isMobileViewport()) getActiveWorkspace()?.close();
  };
  mqMobile.addEventListener("change", onViewportChange);
  mqMobileH.addEventListener("change", onViewportChange);

  return {
    dismissSortToast: dismissSortToastNow,
    restoreWorkspace,
    unmount() {
      offEvents();
      window.clearInterval(pollIntervalId);
      window.removeEventListener("sturddle:settings-changed", onSettingsChanged);
      window.removeEventListener("sturddle:workspace-closed", syncWindowMenu);
      mqMobile.removeEventListener("change", onViewportChange);
      mqMobileH.removeEventListener("change", onViewportChange);
      document.removeEventListener("click", closeMenus);
      dismissSortToastNow();
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

  // Multi-select state per pane: Set of selected IDs + the last clicked
  // "anchor" (used as the start of a Shift+Click range). Anchors live in
  // {id} wrappers so handleListClick can mutate them.
  const availableSelected = new Set();
  const pickedSelected = new Set();
  const availableAnchor = { id: null };
  const pickedAnchor = { id: null };

  function visibleAvailableIds() {
    return available.filter((e) => !pickedIds.includes(e.id)).map((e) => e.id);
  }

  function handleListClick(ev, id, selected, anchorRef, idsInOrder) {
    if (ev.shiftKey && anchorRef.id != null) {
      // Range select from anchor to id (inclusive), in display order.
      const ids = idsInOrder();
      const a = ids.indexOf(anchorRef.id);
      const b = ids.indexOf(id);
      if (a >= 0 && b >= 0) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        selected.clear();
        for (let i = lo; i <= hi; i++) selected.add(ids[i]);
      }
    } else if (ev.ctrlKey || ev.metaKey) {
      // Toggle this row in/out; keep anchor on the toggled row.
      if (selected.has(id)) selected.delete(id);
      else selected.add(id);
      anchorRef.id = id;
    } else {
      // Plain click: collapse to just this row.
      selected.clear();
      selected.add(id);
      anchorRef.id = id;
    }
  }

  function render() {
    availableList.innerHTML = "";
    for (const e of available) {
      if (pickedIds.includes(e.id)) continue;
      const li = document.createElement("li");
      li.className = "ne-item" + (availableSelected.has(e.id) ? " selected" : "");
      li.dataset.id = e.id;
      li.textContent = e.name;
      li.title = e.name;
      li.addEventListener("click", (ev) => {
        handleListClick(ev, e.id, availableSelected, availableAnchor, visibleAvailableIds);
        render();
      });
      li.addEventListener("dblclick", () => doAdd([e.id]));
      availableList.appendChild(li);
    }

    pickedList.innerHTML = "";
    for (const id of pickedIds) {
      const e = byId.get(id);
      if (!e) continue;
      const li = document.createElement("li");
      li.className = "ne-item" + (pickedSelected.has(id) ? " selected" : "");
      li.dataset.id = id;
      li.textContent = e.name;
      li.addEventListener("click", (ev) => {
        handleListClick(ev, id, pickedSelected, pickedAnchor, () => [...pickedIds]);
        render();
      });
      li.addEventListener("dblclick", () => doRemove([id]));
      pickedList.appendChild(li);
    }

    addBtn.disabled = availableSelected.size === 0;
    removeBtn.disabled = pickedSelected.size === 0;
    // Reorder needs a single anchor row; multi-row moves are out of scope.
    const onlyOne = pickedSelected.size === 1;
    const soloIdx = onlyOne ? pickedIds.indexOf([...pickedSelected][0]) : -1;
    upBtn.disabled = !onlyOne || soloIdx <= 0;
    downBtn.disabled = !onlyOne || soloIdx < 0 || soloIdx >= pickedIds.length - 1;
  }

  function doAdd(ids) {
    if (!ids || !ids.length) return;
    // Preserve the available-pane display order when appending.
    const order = visibleAvailableIds();
    const toAdd = order.filter((id) => ids.includes(id) && !pickedIds.includes(id));
    if (!toAdd.length) return;
    pickedIds.push(...toAdd);
    availableSelected.clear();
    pickedSelected.clear();
    for (const id of toAdd) pickedSelected.add(id);
    pickedAnchor.id = toAdd[toAdd.length - 1];
    availableAnchor.id = null;
    render();
    notify();
  }

  function doRemove(ids) {
    if (!ids || !ids.length) return;
    const removed = new Set(ids);
    const firstRemovedIdx = pickedIds.findIndex((id) => removed.has(id));
    pickedIds = pickedIds.filter((id) => !removed.has(id));
    pickedSelected.clear();
    // Surface a sensible new anchor: the row that slid up into the first
    // removed position, or the new last row if we removed the tail.
    if (pickedIds.length) {
      const next = pickedIds[Math.min(firstRemovedIdx, pickedIds.length - 1)];
      pickedSelected.add(next);
      pickedAnchor.id = next;
    } else {
      pickedAnchor.id = null;
    }
    render();
    notify();
  }

  function move(delta) {
    if (pickedSelected.size !== 1) return;
    const id = [...pickedSelected][0];
    const idx = pickedIds.indexOf(id);
    const target = idx + delta;
    if (target < 0 || target >= pickedIds.length) return;
    [pickedIds[idx], pickedIds[target]] = [pickedIds[target], pickedIds[idx]];
    render();
    notify();
  }

  addBtn.addEventListener("click", () => doAdd([...availableSelected]));
  removeBtn.addEventListener("click", () => doRemove([...pickedSelected]));
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
        const ref = { id, name: e.name, cmd: e.path };
        if (Array.isArray(e.args) && e.args.length) ref.args = e.args.slice();
        if (e.env && typeof e.env === "object" && Object.keys(e.env).length) {
          ref.env = { ...e.env };
        }
        return ref;
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
