// Tournaments perspective: master list + "New Tournament" verb.
// Path/defaults configuration lives in the global Settings dialog under
// the "Tournament" tab -- not here.
//
// Row-targeted verbs (Start/Restart, Stop, Open workspace, Info, Remove)
// live in a left-side vertical ribbon that mirrors the Play perspective's
// look and feel. Clicking a row selects it; ribbon actions target the
// selected tournament. New / Sort / Window remain in the top menubar.

import { mqMobile, mqMobileH, mqMobileHPlay } from "./breakpoints.js";
import { apiErrorDetail, buildToastWithActions, confirm, makeToastDismissBtn, OPEN_ENGINES_ACTION, reportError, showDialog, toast } from "./dialogs.js";
import { openSettingsDialog } from "./settings-dialog.js";
import { crashErrorLine, CRASH_TOAST_DURATION_MS, EVT, KIND, POLL_INTERVAL_MS, sprtParamErrors, STATUS } from "./tournament-events.js";
import { APP_EVT } from "./app-events.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw, saveRaw } from "./storage.js";
import { CONFIRM_WIPE_QS, buildRestartConfirm } from "./tournament-restart.js";
import { mountTournamentTemplateForm } from "./tournament-template-form.js";
import { mountSprtButton } from "./tournament-sprt-button.js";
import { clearWorkspaceState, getActiveLayout, getActiveWorkspace, hasSavedWorkspaceState, LAYOUT, openTournamentWorkspace } from "./tournament-workspace.js";
import { renderTournamentRow, totalGames, updateRowProgress } from "./tournament-row.js";
import { debounce, isCtrlA, markSelectable, ribbonWidthPx } from "./wb-utils.js";

const NEED_TWO_ENGINES_MSG = "Register at least 2 engines first.";
const BAD_SPRT_DEFAULTS_MSG = "Invalid SPRT params (need alpha+beta<1, elo0<elo1).";
// Default dwell for error/warning toasts that carry a line worth reading.
const TOAST_DURATION_MS = 8000;
const NEW_TOURNAMENT_LABEL = "New tournament";
const EMPTY_CTA_PREFIX = "No tournaments yet -- click ";
const EMPTY_CTA_SUFFIX = " to create one.";
const REVEAL_DEBOUNCE_MS = 500;
// Unicode ellipsis is intentional: this glyph is rendered into the
// tournament-id span (user-facing), not a code token. ASCII-only rule
// does not apply to surfaced UI text.
const ID_ELLIPSIS = "…";
const VALID_SORTS = new Set(["name", "status", "created_at", "started_at"]);
const IS_LOCAL = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);

const PANEL_HTML = `
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

// ---- Generic async wrappers ---------------------------------------------

// Wraps an async function so concurrent calls are dropped until it resolves.
function guard(fn) {
  let inflight = false;
  return async (...args) => {
    if (inflight) return;
    inflight = true;
    try { await fn(...args); } finally { inflight = false; }
  };
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

// ---- Pure formatters ----------------------------------------------------

function basename(p) {
  if (!p) return p;
  return p.split(/[\\/]/).pop() || p;
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

function formatGames(t) {
  const played = t.standings?.games;
  const total = totalGames(t);
  if (played == null && total == null) return null;
  if (total == null) return String(played ?? 0);
  return `${played ?? 0} of ${total}`;
}

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

// ---- API helpers --------------------------------------------------------

function syncWorkspaceOtherActive(ctx) {
  const ws = getActiveWorkspace();
  if (!ws?.setOtherActive) return;
  if (!ctx.activeId || ctx.activeId === ws.tournamentId) {
    ws.setOtherActive(null, null);
    return;
  }
  const other = ctx.tournaments.find((x) => x.id === ctx.activeId);
  ws.setOtherActive(ctx.activeId, other?.name || null);
}

async function loadGlobalEngineDefaults(ctx) {
  try {
    const s = await ctx.api("GET", "/settings");
    return {
      threads: s.engine_default_threads,
      hash_mb: s.engine_default_hash_mb,
    };
  } catch {
    return { threads: null, hash_mb: null };
  }
}

// ---- Rendering ----------------------------------------------------------

function renderList(ctx) {
  ctx.listEl.innerHTML = "";

  const noFastchess = !ctx.settings || !ctx.settings.fastchess_detected;
  const noTournaments = ctx.tournaments.length === 0;

  if (noFastchess) {
    ctx.emptyEl.classList.remove("hidden");
    ctx.emptyMsg.replaceChildren();
    ctx.emptyMsg.append("fastchess not configured — open ");
    const link = document.createElement("a");
    link.href = "#";
    link.className = "settings-deeplink";
    link.textContent = "Settings → Tournament";
    link.addEventListener("click", (e) => {
      e.preventDefault();
      openSettingsDialog({ api: ctx.api, initialTab: "tournament" });
    });
    ctx.emptyMsg.append(link, " to set the binary path.");
    ctx.newBtn.disabled = true;
    ctx.selectedId = null;
    syncRibbon(ctx);
    return;
  }
  ctx.newBtn.disabled = false;

  if (noTournaments) {
    ctx.emptyEl.classList.remove("hidden");
    ctx.emptyMsg.replaceChildren(...newTournamentCta(() => openNewTournamentDialog(ctx)));
    ctx.selectedId = null;
    syncRibbon(ctx);
    return;
  }
  ctx.emptyEl.classList.add("hidden");

  const sorted = sortedTournaments(ctx);
  if (!ctx.selectedId || !sorted.some((t) => t.id === ctx.selectedId)) {
    ctx.selectedId = (ctx.activeId && sorted.some((t) => t.id === ctx.activeId)) ? ctx.activeId : sorted[0].id;
  }
  for (const t of sorted) {
    ctx.listEl.appendChild(renderRow(ctx, t));
  }
  syncRibbon(ctx);
  if (ctx.initialLoad) {
    ctx.initialLoad = false;
    maybeRestoreWorkspace(ctx);
  }
}

function sortedTournaments(ctx) {
  const arr = ctx.tournaments.slice();
  const dir = ctx.sortAsc ? 1 : -1;
  const cmp = (a, b) => {
    const av = a[ctx.sortBy] ?? "";
    const bv = b[ctx.sortBy] ?? "";
    if (av === bv) return a.created_at.localeCompare(b.created_at);
    // Empty values sink to the bottom regardless of direction.
    if (av === "") return 1;
    if (bv === "") return -1;
    if (ctx.sortBy === "name") return dir * av.localeCompare(bv, undefined, { sensitivity: "base" });
    return dir * (av < bv ? -1 : 1);
  };
  return arr.sort(cmp);
}

function renderRow(ctx, t) {
  return renderTournamentRow(t, {
    selected: t.id === ctx.selectedId,
    onSelect: (t) => {
      ctx.listEl.focus({ preventScroll: true });
      if (ctx.selectedId === t.id) return;
      navigateTo(ctx, t.id);
    },
    onInfo: (t) => ctx.openInfoGuarded(t),
  });
}

function updateProgressInPlace(ctx, t) {
  if (t?.standings?.games == null) return;
  updateRowProgress(ctx.listEl.querySelector(`li[data-id="${t.id}"]`), t);
}

function selectedTournament(ctx) {
  return ctx.tournaments.find((t) => t.id === ctx.selectedId) || null;
}

function syncRibbon(ctx) {
  syncTidyBtn(ctx);
  const t = selectedTournament(ctx);
  if (!t) {
    ctx.ribbonStartBtn.disabled = true;
    ctx.ribbonStopBtn.disabled = true;
    ctx.ribbonWorkspaceBtn.disabled = true;
    ctx.ribbonInfoBtn.disabled = true;
    ctx.ribbonEditBtn.disabled = true;
    ctx.ribbonRemoveBtn.disabled = true;
    if (!ctx.ribbonStartBtn.querySelector("wa-icon")) ctx.ribbonStartBtn.innerHTML = '<wa-icon class="t-start-icon" name="play"></wa-icon>';
    else ctx.ribbonStartBtn.querySelector("wa-icon").setAttribute("name", "play");
    ctx.ribbonStartBtn.setAttribute("aria-label", "Start");
    ctx.ribbonStartBtn.setAttribute("title", "Start");
    return;
  }
  const isActive = t.id === ctx.activeId;
  const anotherRunning = ctx.activeId !== null && !isActive;
  const status = t.status;
  // Stopped/failed -> Start = restart from scratch (Stop is destructive;
  // fastchess's resume contract is fragile across stop/resume cycles).
  const isRestart = status === STATUS.STOPPED || status === STATUS.FAILED;

  // !!ctx.startingId: only one tournament may start at a time (by design).
  ctx.ribbonStartBtn.disabled = isActive || anotherRunning || status === STATUS.RUNNING || status === STATUS.DONE || !!ctx.startingId;
  ctx.ribbonStopBtn.disabled = !isActive;
  ctx.ribbonRemoveBtn.disabled = isActive;
  ctx.ribbonWorkspaceBtn.disabled = !!getActiveWorkspace();
  ctx.ribbonInfoBtn.disabled = false;
  ctx.ribbonEditBtn.disabled = isActive || status === STATUS.DONE;

  const starting = t.id === ctx.startingId;
  const startIconName = isRestart ? "rotate-right" : "play";
  if (starting) {
    ctx.ribbonStartBtn.innerHTML = '<wa-spinner class="spinner-accent"></wa-spinner>';
  } else if (!ctx.ribbonStartBtn.querySelector("wa-icon")) {
    ctx.ribbonStartBtn.innerHTML = `<wa-icon class="t-start-icon" name="${startIconName}"></wa-icon>`;
  } else {
    ctx.ribbonStartBtn.querySelector("wa-icon").setAttribute("name", startIconName);
  }
  const startLabel = isRestart ? "Restart" : "Start";
  ctx.ribbonStartBtn.setAttribute("aria-label", startLabel);
  ctx.ribbonStartBtn.setAttribute("title", startLabel);
  const stopping = t.id === ctx.stoppingId;
  if (stopping) {
    ctx.ribbonStopBtn.disabled = true;
    ctx.ribbonStopBtn.innerHTML = '<wa-spinner class="spinner-accent"></wa-spinner>';
  } else {
    ctx.ribbonStopBtn.innerHTML = '<wa-icon name="hand"></wa-icon>';
  }
}

// ---- Navigation / workspace --------------------------------------------

function dismissSortToastNow(ctx) {
  ctx.dismissSortToast?.();
  ctx.dismissSortToast = null;
  ctx.sortToastTextEl = null;
  ctx.sortToastToggleBtn = null;
  ctx.sortToastHiddenWbs = [];
}

function teardownWorkspace(ctx, ws) {
  dismissSortToastNow(ctx);
  ws.close();
  // Note: if the user closes all windows individually, finalize() fires
  // inside tournament-workspace.js with no callback here, so the sort
  // toast may linger with stale WinBox refs. Harmless (restoreWindows
  // swallows errors), but not covered by this fix.
}

async function navigateTo(ctx, newId) {
  const ws = getActiveWorkspace();
  const hadWorkspace = ws && ws.tournamentId !== newId;
  if (hadWorkspace) {
    // Live game windows close here but are restored when switching back
    // (if the tournament is still running), so no confirmation needed.
    teardownWorkspace(ctx, ws);
  }
  ctx.selectedId = newId;
  for (const el of ctx.listEl.querySelectorAll(".tournament-row.selected")) el.classList.remove("selected");
  const li = ctx.listEl.querySelector(`.tournament-row[data-id="${newId}"]`);
  if (li) {
    li.classList.add("selected");
    li.scrollIntoView({ block: "nearest" });
  }
  syncRibbon(ctx);
  const t = selectedTournament(ctx);
  if (t && hasSavedWorkspaceState(t.id)) {
    // Only auto-open when the tournament has saved workspace state with
    // open windows. Brand-new or explicitly-dismissed tournaments stay
    // closed -- the user can open them manually.
    openWorkspace(ctx, t);
  }
  // Invariant: an open workspace always reflects the selected tournament.
  // The list-level poll relies on this to drive workspace.refresh() from
  // selectedId without having to track the workspace's pinned tid.
  const wsAfter = getActiveWorkspace();
  if (wsAfter && wsAfter.tournamentId !== ctx.selectedId) {
    throw new Error(`workspace tid ${wsAfter.tournamentId} != selectedId ${ctx.selectedId}`);
  }
  return true;
}

function openWorkspace(ctx, t) {
  const menubar = ctx.container.querySelector(".tournaments-menubar");
  const ribbon = ctx.container.querySelector(".tournaments-ribbon");
  const rect = menubar.getBoundingClientRect();
  const ribbonRect = ribbon ? ribbon.getBoundingClientRect() : null;
  // Reserve the ribbon's width on BOTH edges regardless of which side
  // it docks to (or whether it's floating). Keeps the workspace symmetric
  // and ribbon-side-flips don't reshape the available area.
  // Prefer the measured ribbon rect; zero width means the in-place ribbon
  // is hidden (floating mode), so fall back to the --ribbon-w CSS var.
  const ribbonW = ribbonRect?.width > 0 ? Math.round(ribbonRect.width) : ribbonWidthPx(".tournaments-body");
  const top = Math.round(rect.bottom);
  const left = Math.max(Math.round(rect.left), ribbonW);
  const getRight = () => window.innerWidth - ribbonW;
  openTournamentWorkspace({ api: ctx.api, events: ctx.events, log: ctx.log, token: ctx.token, tournament: t, top, left, getRight });
  // Seed the workspace's view of the other-active tournament so the
  // banner Restart button reflects busy state on open, not just after
  // the next loadList tick.
  syncWorkspaceOtherActive(ctx);
  syncWindowMenu(ctx);
  syncRibbon(ctx);
}

// ---- Verbs --------------------------------------------------------------

async function startOne(ctx, t) {
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
    await ctx.api("POST", `/api/tournaments/${t.id}/start${qs}`);
  } catch (e) {
    reportError({ log: ctx.log }, `Starting "${t.name}" failed`, e);
    return;
  }
  await ctx.loadList();
}

async function stopOne(ctx, t) {
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
    await ctx.api("POST", `/api/tournaments/${t.id}/stop`);
  } catch (e) {
    reportError({ log: ctx.log }, `Stopping "${t.name}" failed`, e);
  }
  await ctx.loadList();
}

async function removeOne(ctx, t) {
  const ok = await confirm({
    message: `Remove "${t.name}"?\nAll games and data will be permanently deleted.`,
    okLabel: "Remove",
    destructive: true,
    messageClass: "confirm-message--multiline",
  });
  if (!ok) return;
  try {
    await ctx.api("DELETE", `/api/tournaments/${t.id}`);
    if (getActiveWorkspace()?.tournamentId === t.id) getActiveWorkspace().close();
    clearWorkspaceState(t.id);
    toast(`Removed tournament "${t.name}"`, { variant: "success" });
  } catch (e) {
    reportError({ log: ctx.log }, `Removing "${t.name}" failed`, e);
    return;
  }
  await ctx.loadList();
}

// Shared tournament verbs for other UIs (e.g. Studio). The action functions
// only read {api, log, settings, loadList} off ctx, so a minimal ctx adapter
// lets a different perspective reuse them with no change to the verbs.
export function tournamentActions({ api, log, getSettings, reload }) {
  const ctx = {
    api, log, loadList: reload,
    get settings() { return getSettings ? getSettings() : null; },
  };
  return {
    create: () => openNewTournamentDialog(ctx),
    edit: (t) => openEditTournamentDialog(ctx, t),
    info: (t) => openInfoDialog(ctx, t),
    start: (t) => startOne(ctx, t),
    stop: (t) => stopOne(ctx, t),
    remove: (t) => removeOne(ctx, t),
  };
}

// Empty-list call to action shared by Arena and Studio: prose wrapping an
// inline "+" button that fires New. Returns the nodes to append into a host
// (text, button, text) so each perspective drops them into its own container.
export function newTournamentCta(onCreate) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "toast-icon-btn";
  btn.setAttribute("aria-label", NEW_TOURNAMENT_LABEL);
  btn.setAttribute("title", NEW_TOURNAMENT_LABEL);
  const ic = document.createElement("wa-icon");
  ic.setAttribute("name", "plus");
  btn.appendChild(ic);
  btn.addEventListener("click", () => onCreate());
  return [EMPTY_CTA_PREFIX, btn, EMPTY_CTA_SUFFIX];
}

// ---- Info dialog --------------------------------------------------------

async function openInfoDialog(ctx, t) {
  let detailed = t;
  try {
    detailed = await ctx.api("GET", `/api/tournaments/${t.id}`);
  } catch (e) {
    ctx.log?.("Loading tournament details failed:", e);
  }
  showDialog({
    label: detailed.name,
    width: "520px",
    body: (resolve, dialog) => {
      dialog.classList.add("tournament-info-dialog");
      const wrap = document.createElement("div");
      wrap.className = "tournament-info";
      wrap.appendChild(buildInfoContent(ctx, detailed));
      dialog.appendChild(wrap);
    },
  });
}

function buildInfoContent(ctx, t) {
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
  if (ctx.settings?.tournaments_root) {
    const folder = ctx.settings.tournaments_root.replace(/[\\/]+$/, "") + "/" + t.id;
    if (IS_LOCAL) {
      const btn = document.createElement("button");
      btn.className = "tournament-info-reveal-btn";
      btn.title = folder;
      btn.innerHTML = `<wa-icon name="folder-open"></wa-icon>`;
      btn.addEventListener("click", debounce(async () => {
        try {
          await ctx.api("POST", `/api/tournaments/${t.id}/reveal`);
        } catch (e) {
          reportError({ log: ctx.log }, "Could not open folder", e);
        }
      }, REVEAL_DEBOUNCE_MS));
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
    row("SPRT", `elo0=${s.elo0} elo1=${s.elo1} alpha=${s.alpha} beta=${s.beta}`);
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
  row("Tablebase adjudication", tpl.tb_adjudication ? "On" : "Off");
  const ed = t.engine_defaults || {};
  const defaultSpan = (text, title) => {
    const span = document.createElement("span");
    if (text != null && text !== "") {
      span.textContent = text;
      if (title) span.title = title;
    } else {
      span.textContent = "engine default";
      span.className = "tournament-info-default";
    }
    return span;
  };
  row("Threads", defaultSpan(ed.threads));
  row("Hash (MB)", defaultSpan(ed.hash_mb));
  {
    const span = ed.syzygy_path
      ? defaultSpan(basename(ed.syzygy_path), ed.syzygy_path)
      : defaultSpan(null);
    row("Syzygy", span);
  }
  {
    const span = ed.book_path
      ? defaultSpan(basename(ed.book_path), ed.book_path)
      : defaultSpan(null);
    row("Opening book", span);
    if (ed.book_path) {
      row("Book plies", ed.book_plies);
      row("Book order", ed.book_order);
    }
  }
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

// ---- New / Edit Tournament dialogs --------------------------------------

// Shared dialog body for both create and edit flows.
// Returns a Promise that resolves to {name, template, engines} or null.
async function openTournamentDialog(ctx, { label, actionLabel, initialName, initialEngines, initialTemplate, available, onSubmit }) {
  // Read the saved defaults fresh from the server store here, not from a
  // per-view settings cache: Arena and Studio refresh that cache differently
  // (Studio not at all), so the shared dialog must own its source of truth.
  // Edit passes initialTemplate (the frozen template), which still wins.
  const saved = await ctx.api("GET", "/api/tournament-settings").catch(() => ({}));
  const defaults =
    initialTemplate ||
    saved.default_template ||
    { tc: "10+0.1", rounds: 10, games_in_parallel: 1 };
  const sprtDefaults = saved.sprt_defaults;

  return showDialog({
    label,
    width: "min(660px, 94vw)",
    defaultValue: null,
    body: (resolve, dialog) => {
      const wrap = document.createElement("div");
      wrap.className = "new-tournament-form";
      wrap.innerHTML = `
        <div class="nt-name-row">
          <wa-input class="nt-name" label="Name" size="small" placeholder="my tournament"></wa-input>
          <div class="nt-sprt-host"></div>
        </div>

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
        syzygyPath: saved.engine_default_syzygy_path || "",
      });

      const sprtCtl = mountSprtButton({
        host: wrap.querySelector(".nt-sprt-host"),
        initialSprt: defaults.sprt,
        sprtDefaults,
        onChange: (on) => tplCtl.applySprt(on),
        // Write the last-used SPRT params straight to the server store (the
        // single source of truth); the next dialog open re-reads them.
        onPersist: (sprt_defaults) =>
          ctx.api("PUT", "/api/tournament-settings", { sprt_defaults })
            .catch((e) => reportError({ log: ctx.log }, "Saving SPRT defaults failed", e)),
      });
      // Reflect an SPRT template (edit flow) into the form's enable state.
      tplCtl.applySprt(sprtCtl.isOn());

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
      sprtCtl.setAvailable(builder.getEngines().length === 2);
      nameInput.addEventListener("input", refreshValidity);
      builder.onChange(() => {
        sprtCtl.setAvailable(builder.getEngines().length === 2);
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
        // SPRT params come from the chip popup (validated there before Done);
        // re-check defensively before the destructive create/apply.
        if (sprtCtl.isOn()) {
          const sprtParams = sprtCtl.getParams();
          if (sprtParamErrors(sprtParams).size) {
            toast(BAD_SPRT_DEFAULTS_MSG, { variant: "danger", duration: TOAST_DURATION_MS });
            return;
          }
          template.sprt = sprtParams;
        }

        const picked = builder.getPickedRegistry();
        const globalDefaults = await loadGlobalEngineDefaults(ctx);
        const resolved = resolveResourceParams(template, picked, globalDefaults);
        actionBtn.loading = true;
        let rescheckResult;
        try {
          rescheckResult = await ctx.api("POST", "/api/tournaments/rescheck", resolved);
        } catch (e) {
          const detail = apiErrorDetail(e);
          const msg = (detail && detail.message) || detail || "Resource check failed";
          toast(typeof msg === "string" ? msg : String(msg), {
            variant: "danger", duration: TOAST_DURATION_MS,
          });
          actionBtn.loading = false;
          return;
        } finally {
          actionBtn.loading = false;
        }
        if (rescheckResult.warnings && rescheckResult.warnings.length) {
          for (const w of rescheckResult.warnings) {
            toast(`Warning: ${w.message}`, { variant: "warning", duration: TOAST_DURATION_MS });
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

async function openNewTournamentDialog(ctx) {
  let registry;
  try {
    registry = await ctx.api("GET", "/engines");
  } catch (e) {
    reportError({ log: ctx.log }, "Loading engine registry failed", e);
    return;
  }
  const available = registry.engines || [];
  if (available.length < 2) {
    toast(buildToastWithActions(NEED_TWO_ENGINES_MSG, [OPEN_ENGINES_ACTION]), { variant: "danger" });
    return;
  }

  await openTournamentDialog(ctx, {
    label: "New tournament",
    actionLabel: "Create",
    initialName: "",
    initialEngines: [],
    // No initialTemplate: New has no frozen template. The dialog prefills
    // from the saved default_template it reads fresh from the server, so the
    // popup never depends on a per-view settings cache (Edit passes the
    // existing tournament's frozen template, which does win).
    initialTemplate: null,
    available,
    onSubmit: async (data) => {
      try {
        await ctx.api("POST", "/api/tournaments", data);
        toast(`Created new tournament "${data.name}"`, { variant: "success" });
      } catch (e) {
        reportError({ log: ctx.log }, "Creating tournament failed", e);
        if (/-> 409\b/.test(e.message)) throw Object.assign(e, { isNameCollision: true });
        return false;
      }
      await ctx.loadList();
    },
  });
}

async function openEditTournamentDialog(ctx, t) {
  // PRE-OPEN gate: warn early so the user can bail without loading the
  // registry or filling the dialog. Keep this even though there is also a
  // post-dialog confirm -- the two guards serve different purposes: this one
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
    registry = await ctx.api("GET", "/engines");
  } catch (e) {
    reportError({ log: ctx.log }, "Loading engine registry failed", e);
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
      `${droppedCount} engine${droppedCount === 1 ? "" : "s"} no longer in the registry -- re-add before applying.`,
      { variant: "warning", duration: TOAST_DURATION_MS },
    );
  }

  await openTournamentDialog(ctx, {
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
      // and must not mix with future games -- so this fires on hasGames alone.
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
        await ctx.api("PATCH", `/api/tournaments/${t.id}`, data);
        toast(`Updated "${data.name}"`, { variant: "success" });
      } catch (e) {
        reportError({ log: ctx.log }, "Updating tournament failed", e);
        if (/-> 409\b/.test(e.message)) throw Object.assign(e, { isNameCollision: true });
        failed = true;
      }
      // Refresh either way: success applied changes; failure may indicate the
      // local view drifted (e.g. tournament started elsewhere) and should
      // re-sync.
      await ctx.loadList();
      if (failed) return false;
    },
  });
}

// ---- Window / sort menus ------------------------------------------------

function syncWindowMenu(ctx) {
  ctx.windowMenuBtn.disabled = !getActiveWorkspace();
}

function closeMenus(ctx) {
  ctx.container.querySelectorAll(".tmb-menu.open").forEach(m => m.classList.remove("open"));
}

function syncSortMenu(ctx) {
  for (const opt of ctx.container.querySelectorAll(".tmb-sort-opt")) {
    opt.classList.toggle("is-active", opt.dataset.sort === ctx.sortBy);
  }
  for (const opt of ctx.container.querySelectorAll(".tmb-sort-opt")) {
    const active = opt.dataset.sort === ctx.sortBy;
    if (active) opt.dataset.dir = ctx.sortAsc ? "asc" : "desc";
    else delete opt.dataset.dir;
  }
}

function applySort(ctx, nextBy, nextAsc) {
  ctx.sortBy = nextBy;
  ctx.sortAsc = nextAsc;
  saveRaw(STORAGE_KEY.TOURNAMENTS_SORT_BY, ctx.sortBy);
  saveRaw(STORAGE_KEY.TOURNAMENTS_SORT_ASC, String(ctx.sortAsc));
  syncSortMenu(ctx);
  renderList(ctx);
}

// Persistent sort toast -- reuse DOM in place to avoid flicker on re-sort.
function ensureSortToast(ctx, ws) {
  if (ctx.dismissSortToast) return;
  const msg = document.createElement("span");
  msg.className = "toast-sort-msg";
  ctx.sortToastTextEl = document.createElement("span");
  ctx.sortToastToggleBtn = document.createElement("button");
  ctx.sortToastToggleBtn.className = "toast-action-btn toast-ws-toggle toast-ws-minimize";
  ctx.sortToastHidden = false;
  ctx.sortToastHiddenWbs = [];
  ctx.sortToastToggleBtn.addEventListener("click", () => {
    if (!ctx.sortToastHidden) {
      ctx.sortToastHiddenWbs = ws.minimizeAll();
      ctx.sortToastToggleBtn.classList.replace("toast-ws-minimize", "toast-ws-restore");
      ctx.sortToastHidden = true;
    } else {
      // Restore is the toast's terminal action: once the user has
      // un-minimized the windows they minimized, the toast has served
      // its purpose. Dismissing avoids a stale "sorted by..." linger.
      ws.restoreWindows(ctx.sortToastHiddenWbs);
      dismissSortToastNow(ctx);
    }
  });
  const closeBtn = makeToastDismissBtn(() => dismissSortToastNow(ctx));
  msg.append(ctx.sortToastTextEl, ctx.sortToastToggleBtn, closeBtn);
  ctx.dismissSortToast = toast(msg, { duration: 0 });
}

function syncTidyBtn(ctx) {
  const layout = getActiveLayout();
  ctx.snapBtn.classList.toggle("tmb-active", layout === LAYOUT.SNAP);
  ctx.tileBtn.classList.toggle("tmb-active", layout === LAYOUT.TILE);
  ctx.tidyBtn.classList.toggle("tmb-active", layout === LAYOUT.TIDY);
}

// ---- Live updates from WS -----------------------------------------------

function onWsEvent(ctx, evt) {
  if (evt.kind !== EVT.STATUS && evt.kind !== EVT.UPDATE) return;
  // Surface runner crashes as a toast -- the user may not have a
  // workspace open and would otherwise see the row silently flip
  // to a terminal state with no explanation.
  const inner = evt.payload?.kind;
  if (inner === KIND.RUNNER_CRASH) {
    const tid = evt.payload?.tournament_id;
    const t = ctx.tournaments.find((x) => x.id === tid);
    const name = t ? t.name : "Tournament";
    toast(`${name} failed: ${crashErrorLine(evt.payload)}`, {
      variant: "danger", duration: CRASH_TOAST_DURATION_MS,
    });
  }
  // The /start API doesn't return until orchestrator.start completes
  // (which can include a multi-second PGN rewrite); the status event
  // fires earlier. Clear pending flags here, with an optimistic local
  // status update so syncRibbon reflects the transition immediately.
  const tid = evt.payload?.tournament_id;
  const newStatus = evt.payload?.status;
  const t = tid ? ctx.tournaments.find((x) => x.id === tid) : null;
  if (t && newStatus) {
    t.status = newStatus;
    if (newStatus === STATUS.RUNNING) ctx.activeId = tid;
    else if (ctx.activeId === tid) ctx.activeId = null;
  }
  if (ctx.startingId === tid && newStatus === STATUS.RUNNING) {
    ctx.startingId = null;
  }
  if (
    ctx.stoppingId === tid &&
    [STATUS.STOPPED, STATUS.DONE, STATUS.FAILED].includes(newStatus)
  ) {
    ctx.stoppingId = null;
  }
  // Re-render with the optimistic state; debouncedLoadList canonicalizes.
  renderList(ctx);
  syncWorkspaceOtherActive(ctx);
  ctx.debouncedLoadList();
}

// Single periodic refresh for the selected tournament when it's running.
// Hits one endpoint per tick and fans out: list progress bar in place,
// and the workspace (if open) via applyDetail() so it doesn't re-fetch.
// navigateTo's invariant guarantees workspace.tournamentId === selectedId
// when a workspace is open, so we can drive both from selectedId alone.
async function pollTick(ctx) {
  const t = selectedTournament(ctx);
  if (!t || t.status !== STATUS.RUNNING) return;
  let fresh;
  try {
    fresh = await ctx.api("GET", `/api/tournaments/${t.id}`);
  } catch (e) {
    ctx.log?.(`tournaments poll failed: ${e.message}`);
    return;
  }
  // Mutate in place so renderList() / sort / etc. see the latest.
  // Narrow copy: list only consumes status + standings; workspace-only
  // fields stay out of tournaments[] to avoid stale-field confusion.
  t.status = fresh.status;
  t.standings = fresh.standings;
  updateProgressInPlace(ctx, t);
  const ws = getActiveWorkspace();
  if (ws && ws.tournamentId === t.id) ws.applyDetail(fresh);
}

// ---- Workspace restore --------------------------------------------------

// In-place ribbon goes horizontal/full-width below 800w/680h (styles.css
// mobile block); openWorkspace would measure it as the side inset and
// shove every window to the right edge. Floating ribbon never mismeasures.
function ribbonMobileHorizontal() {
  return (mqMobile.matches || mqMobileHPlay.matches) && !document.body.dataset.ribbonFloat;
}

function maybeRestoreWorkspace(ctx) {
  if (!ctx.tournamentsTabActive || ctx.initialLoad) return;
  if (ribbonMobileHorizontal()) return;
  const t = selectedTournament(ctx);
  if (t && hasSavedWorkspaceState(t.id)) openWorkspace(ctx, t);
}

function restoreWorkspace(ctx) {
  ctx.tournamentsTabActive = true;
  // Defer one frame so the tab panel is laid out before openWorkspace
  // measures ribbon/menubar geometry via getBoundingClientRect().
  requestAnimationFrame(() => maybeRestoreWorkspace(ctx));
}

// ---- Wiring -------------------------------------------------------------

function wireRibbon(ctx) {
  ctx.ribbonStartBtn.addEventListener("click", async () => {
    const t = selectedTournament(ctx);
    if (!t || ctx.ribbonStartBtn.disabled || ctx.startingId) return;
    ctx.startingId = t.id;
    syncRibbon(ctx);
    try { await startOne(ctx, t); } finally { ctx.startingId = null; syncRibbon(ctx); }
  });
  ctx.ribbonStopBtn.addEventListener("click", async () => {
    const t = selectedTournament(ctx);
    if (!t || ctx.ribbonStopBtn.disabled || ctx.stoppingId) return;
    ctx.stoppingId = t.id;
    syncRibbon(ctx);
    try { await stopOne(ctx, t); } finally { ctx.stoppingId = null; syncRibbon(ctx); }
  });
  ctx.ribbonWorkspaceBtn.addEventListener("click", () => {
    const t = selectedTournament(ctx);
    if (t) openWorkspace(ctx, t);
  });
  ctx.ribbonInfoBtn.addEventListener("click", () => {
    const t = selectedTournament(ctx);
    if (t) ctx.openInfoGuarded(t);
  });
  ctx.ribbonEditBtn.addEventListener("click", () => {
    const t = selectedTournament(ctx);
    if (t && !ctx.ribbonEditBtn.disabled) openEditTournamentDialog(ctx, t);
  });
  ctx.ribbonRemoveBtn.addEventListener("click", () => {
    const t = selectedTournament(ctx);
    if (t && !ctx.ribbonRemoveBtn.disabled) ctx.removeOneGuarded(t);
  });
}

function wireListKeyboard(ctx) {
  ctx.listEl.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp" && ev.key !== "Home" && ev.key !== "End") return;
    const sorted = sortedTournaments(ctx);
    if (sorted.length === 0) return;
    const cur = sorted.findIndex((t) => t.id === ctx.selectedId);
    let next = cur;
    if (ev.key === "ArrowDown") next = cur < 0 ? 0 : Math.min(cur + 1, sorted.length - 1);
    else if (ev.key === "ArrowUp") next = cur < 0 ? sorted.length - 1 : Math.max(cur - 1, 0);
    else if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = sorted.length - 1;
    if (next === cur) { ev.preventDefault(); return; }
    ev.preventDefault();
    navigateTo(ctx, sorted[next].id);
  });
}

function wireMenus(ctx) {
  ctx.sortMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = ctx.sortMenu.classList.contains("open");
    closeMenus(ctx);
    if (!isOpen) ctx.sortMenu.classList.add("open");
  });
  for (const opt of ctx.container.querySelectorAll(".tmb-sort-opt")) {
    opt.addEventListener("click", () => {
      const next = opt.dataset.sort;
      if (!VALID_SORTS.has(next)) { closeMenus(ctx); return; }
      const nextAsc = next === ctx.sortBy ? !ctx.sortAsc : ctx.sortAsc;
      applySort(ctx, next, nextAsc);
      const label = `Tournaments sorted by ${opt.textContent.trim()}, ${nextAsc ? "ascending" : "descending"}`;
      const ws = getActiveWorkspace();
      if (ws) {
        ensureSortToast(ctx, ws);
        ctx.sortToastTextEl.textContent = label;
      } else {
        toast(label);
      }
      closeMenus(ctx);
    });
  }

  ctx.windowMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (ctx.windowMenuBtn.disabled) return;
    const isOpen = ctx.windowMenu.classList.contains("open");
    closeMenus(ctx);
    if (!isOpen) ctx.windowMenu.classList.add("open");
  });

  ctx.snapBtn.addEventListener("click", () => {
    closeMenus(ctx);
    const ws = getActiveWorkspace();
    if (!ws) return;
    if (getActiveLayout() === LAYOUT.SNAP) ws.untidy(); else ws.snap();
    syncTidyBtn(ctx);
  });
  ctx.tileBtn.addEventListener("click", () => {
    closeMenus(ctx);
    const ws = getActiveWorkspace();
    if (!ws) return;
    if (getActiveLayout() === LAYOUT.TILE) ws.untidy(); else ws.tile();
    syncTidyBtn(ctx);
  });
  ctx.tidyBtn.addEventListener("click", () => {
    closeMenus(ctx);
    const ws = getActiveWorkspace();
    if (!ws) return;
    if (getActiveLayout() === LAYOUT.TIDY) ws.untidy(); else ws.tidy();
    syncTidyBtn(ctx);
  });
  ctx.container.querySelector(".tmb-closeall").addEventListener("click", () => {
    closeMenus(ctx);
    const ws = getActiveWorkspace();
    if (ws) { dismissSortToastNow(ctx); ws.closeAll(); }
    syncWindowMenu(ctx);
  });
  for (const [cls, key] of [
    [".tmb-sys-standings", "standings"],
    [".tmb-sys-schedule",  "schedule"],
    [".tmb-sys-engines",   "engines"],
    [".tmb-sys-log",       "log"],
  ]) {
    ctx.container.querySelector(cls).addEventListener("click", () => {
      closeMenus(ctx);
      getActiveWorkspace()?.openSystemWindow(key);
    });
  }
}

// oversized-ok removed: factory decomposed into module-level helpers below.
export function mountTournaments({ container, api, events, log, token }) {
  container.innerHTML = PANEL_HTML;

  const ctx = {
    container, api, events, log, token,
    newBtn: container.querySelector(".t-new"),
    windowMenu: container.querySelector(".tmb-window-menu"),
    windowMenuBtn: container.querySelector(".tmb-window-btn"),
    sortMenu: container.querySelector(".tmb-sort-menu"),
    sortMenuBtn: container.querySelector(".tmb-sort-btn"),
    listEl: container.querySelector(".tournaments-list"),
    emptyEl: container.querySelector(".tournaments-empty"),
    emptyMsg: container.querySelector(".tournaments-empty .empty-message"),
    ribbonStartBtn: container.querySelector(".t-start"),
    ribbonStopBtn: container.querySelector(".t-stop"),
    ribbonWorkspaceBtn: container.querySelector(".t-workspace"),
    ribbonInfoBtn: container.querySelector(".t-info"),
    ribbonEditBtn: container.querySelector(".t-edit"),
    ribbonRemoveBtn: container.querySelector(".t-remove"),
    snapBtn: container.querySelector(".tmb-snap"),
    tileBtn: container.querySelector(".tmb-tile"),
    tidyBtn: container.querySelector(".tmb-tidy"),

    // { fastchess_path, tournaments_root, default_template, fastchess_detected }
    settings: null,
    tournaments: [],
    activeId: null,
    selectedId: null,
    initialLoad: true,
    stoppingId: null,
    startingId: null,
    sortBy: VALID_SORTS.has(loadRaw(STORAGE_KEY.TOURNAMENTS_SORT_BY))
      ? loadRaw(STORAGE_KEY.TOURNAMENTS_SORT_BY) : "created_at",
    sortAsc: loadRaw(STORAGE_KEY.TOURNAMENTS_SORT_ASC) !== "false",

    // Persistent sort toast (reused in place to avoid flicker on re-sort).
    dismissSortToast: null,
    sortToastTextEl: null,
    sortToastToggleBtn: null,
    sortToastHidden: false,
    sortToastHiddenWbs: [],

    tournamentsTabActive: false,
  };
  markSelectable(ctx.listEl, { rows: ".tournament-row" });

  ctx.loadSettings = lastWriteWins(
    () => ctx.api("GET", "/api/tournament-settings"),
    (data) => { ctx.settings = data; renderList(ctx); },
    (e) => reportError({ log }, "Loading tournament settings failed", e),
  );
  ctx.loadList = lastWriteWins(
    () => ctx.api("GET", "/api/tournaments"),
    (body) => {
      ctx.tournaments = body.tournaments;
      ctx.activeId = body.active_id;
      renderList(ctx);
      syncWorkspaceOtherActive(ctx);
    },
    (e) => reportError({ log }, "Loading tournaments failed", e),
  );
  ctx.debouncedLoadList = debounce(ctx.loadList, 150);
  ctx.removeOneGuarded = guard((t) => removeOne(ctx, t));
  ctx.openInfoGuarded = guard((t) => openInfoDialog(ctx, t));

  wireRibbon(ctx);
  wireListKeyboard(ctx);
  wireMenus(ctx);
  ctx.newBtn.addEventListener("click", () => openNewTournamentDialog(ctx));
  syncSortMenu(ctx);

  ctx.onMenuDocClick = () => closeMenus(ctx);
  document.addEventListener("click", ctx.onMenuDocClick);

  ctx.onWsEvent = (evt) => onWsEvent(ctx, evt);
  const offEvents = ctx.events.on(ctx.onWsEvent);

  // Settings can change in another tab/dialog -- pick those up too.
  ctx.onSettingsChanged = () => ctx.loadSettings();
  ctx.onWorkspaceClosed = () => { syncWindowMenu(ctx); syncRibbon(ctx); };
  window.addEventListener(APP_EVT.SETTINGS_CHANGED, ctx.onSettingsChanged);
  window.addEventListener(APP_EVT.WORKSPACE_CLOSED, ctx.onWorkspaceClosed);

  const pollIntervalId = window.setInterval(() => pollTick(ctx), POLL_INTERVAL_MS);

  syncWindowMenu(ctx);
  // Visibility is driven by the Engines tab group (see engines.js):
  // the workspace stays hidden unless the Tournaments sub-tab is active.

  // Fire-and-forget: lastWriteWins resolves undefined; state is populated
  // asynchronously and rendered via renderList() inside each commit.
  ctx.loadSettings();
  // After initial population, fire one immediate pollTick so a workspace
  // revealed on perspective re-mount catches up without waiting a full
  // POLL_INTERVAL_MS. No-op when nothing's running.
  ctx.loadList().then(() => pollTick(ctx));

  // Workspace windows don't fit a mobile viewport in either axis. Width
  // OR height crossing the threshold counts as mobile.
  // Mobile viewport closes the workspace (snapshot stays restorable);
  // widening back to desktop reopens it from that snapshot. Restore is
  // gated on the docked ribbon being vertical again (see
  // ribbonMobileHorizontal); the mqMobileHPlay listener re-fires the
  // deferred restore when height crosses that CSS flip.
  ctx.onViewportChange = () => {
    if (mqMobile.matches || mqMobileH.matches) {
      getActiveWorkspace()?.close();
    } else {
      // rAF: let the desktop layout settle before openWorkspace
      // measures ribbon/menubar geometry.
      requestAnimationFrame(() => maybeRestoreWorkspace(ctx));
    }
  };
  mqMobile.addEventListener("change", ctx.onViewportChange);
  mqMobileH.addEventListener("change", ctx.onViewportChange);
  mqMobileHPlay.addEventListener("change", ctx.onViewportChange);

  return {
    dismissSortToast: () => dismissSortToastNow(ctx),
    restoreWorkspace: () => restoreWorkspace(ctx),
    unmount() {
      offEvents();
      window.clearInterval(pollIntervalId);
      window.removeEventListener(APP_EVT.SETTINGS_CHANGED, ctx.onSettingsChanged);
      window.removeEventListener(APP_EVT.WORKSPACE_CLOSED, ctx.onWorkspaceClosed);
      mqMobile.removeEventListener("change", ctx.onViewportChange);
      mqMobileH.removeEventListener("change", ctx.onViewportChange);
      mqMobileHPlay.removeEventListener("change", ctx.onViewportChange);
      document.removeEventListener("click", ctx.onMenuDocClick);
      dismissSortToastNow(ctx);
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
//             Add -> / <- Remove move engines between panes.
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

  for (const [list, selected, anchorRef, idsInOrder] of [
    [availableList, availableSelected, availableAnchor, visibleAvailableIds],
    [pickedList, pickedSelected, pickedAnchor, () => [...pickedIds]],
  ]) {
    // Ctrl/Cmd+A selects the whole pane as a multi-select set (not text).
    list.addEventListener("keydown", (ev) => {
      if (!isCtrlA(ev)) return;
      ev.preventDefault();
      const ids = idsInOrder();
      if (!ids.length) return;
      selected.clear();
      for (const id of ids) selected.add(id);
      anchorRef.id = ids[ids.length - 1];
      render();
    });
    // A Shift/Ctrl+click otherwise paints a text selection across the pane on
    // top of toggling the row. Cancel the text-drag gesture so the click only
    // drives row selection -- but preventDefault also drops focus, so refocus
    // the pane explicitly or a following Ctrl+A keydown would never reach it.
    list.addEventListener("mousedown", (ev) => {
      if (ev.shiftKey || ev.ctrlKey || ev.metaKey) {
        ev.preventDefault();
        list.focus();
      }
    });
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
      // picked engines -- used by the rescheck resolver.
      return pickedIds.map((id) => byId.get(id)).filter(Boolean);
    },
    onChange(fn) { listeners.add(fn); },
  };
}
