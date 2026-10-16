// Tournaments perspective: master list + "New Tournament" verb.
// Path/defaults configuration lives in the global Settings dialog under
// the "Tournament" tab -- not here.
//
// Row-targeted verbs (Start/Restart, Stop, Open workspace, Info, Remove)
// live in a left-side vertical ribbon that mirrors the Play perspective's
// look and feel. Clicking a row selects it; ribbon actions target the
// selected tournament. New / Sort / Window remain in the top menubar.

import { mqMobile, mqMobileH, mqMobileHPlay } from "./breakpoints.js";
import { apiErrorDetail, buildToastWithActions, confirm, makeToastDismissBtn, OPEN_ENGINES_ACTION, reportError, showDialog, toast, TOAST_DURATION_MS } from "./dialogs.js";
import { openSettingsDialog } from "./settings-dialog.js";
import { crashErrorLine, CRASH_TOAST_DURATION_MS, EVT, KIND, POLL_INTERVAL_MS, sprtParamErrors, STATUS, STATUS_RANK } from "./tournament-events.js";
import { APP_EVT } from "./app-events.js";
import { baseCompare, scrollSortedRowIntoView } from "./col-sort.js";
import { rankCmp } from "./sort-stack.js";
import { wireInlineSearch } from "./inline-search.js";
import { attachRibbonOverflow } from "./ribbon-overflow.js";
import { attachMenuNav, focusFirstItem, MENU_OPEN_CLASS } from "./menu-nav.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw, saveRaw } from "./storage.js";
import {
  buildEngineMatcher,
  engineRefFromRegistry,
  gatedStart,
  loadGlobalEngineDefaults,
  resolveResourceParams,
} from "./tournament-restart.js";
import { BOOK_KEYS, mountTournamentTemplateForm } from "./tournament-template-form.js";
import { makePathRow } from "./settings-path-row.js";
import { mountSprtButton } from "./tournament-sprt-button.js";
import { formatDraw, formatResign, formatStamp, formatType } from "./tournament-format.js";
import { clearWorkspaceState, getActiveLayout, getActiveWorkspace, hasSavedWorkspaceState, LAYOUT, openTournamentWorkspace, STANDINGS_VIEW } from "./tournament-workspace.js";
import { renderTournamentRow, totalGames, updateRowProgress } from "./tournament-row.js";
import { applyListClick, createMultiSelect, selectionTargets } from "./multi-select.js";
import { basename, cooldown, cssVarPx, debounce, guard, isCtrlA, markSelectable, ribbonWidthPx, suppressModifierClickSelect, suppressMultiClickSelect, wireArrowKeyNav } from "./wb-utils.js";

const NEED_TWO_ENGINES_MSG = "Register at least 2 engines first.";
const BAD_SPRT_DEFAULTS_MSG = "Invalid SPRT params (need alpha+beta<1, elo0<elo1).";
// Ribbon tooltips; New/Edit/Copy double as their dialog titles. Studio
// shares them through ribbonHtml()/setStartVerb(), not direct imports.
const NEW_TOURNAMENT_LABEL = "New tournament";
const START_TOURNAMENT_LABEL = "Start tournament";
const RESTART_TOURNAMENT_LABEL = "Restart tournament";
const STOP_TOURNAMENT_LABEL = "Stop tournament";
const OPEN_WORKSPACE_LABEL = "Open workspace";
const TOURNAMENT_DETAILS_LABEL = "Tournament details";
const EDIT_TOURNAMENT_LABEL = "Edit tournament";
const COPY_TOURNAMENT_LABEL = "Copy tournament";
const DELETE_TOURNAMENT_LABEL = "Delete tournament";
const SEARCH_TOURNAMENTS_LABEL = "Search tournaments";
const SEARCH_PLACEHOLDER = "Search tournaments...";
const NO_MATCH_MSG = "No matching tournaments.";
const SEARCH_MINIMIZED_MSG = "Windows minimized while searching";
const RESTORE_WINDOWS_LABEL = "Restore windows";
const SELECTED_ROW_SEL = ".tournament-row.selected";
const CREATE_ACTION_LABEL = "Create";
const REMOVE_ACTION_LABEL = "Remove";
const DELETE_WARNING = "All games and data will be permanently deleted.";
const CONFIRM_MULTILINE_CLASS = "confirm-message--multiline";
const COPY_SUFFIX = " (copy)";
const EMPTY_CTA_PREFIX = "No tournaments yet -- click ";
const EMPTY_CTA_SUFFIX = " to create one.";
const SHOW_STANDINGS_TITLE = "Show standings";
const REVEAL_COOLDOWN_MS = 1500;
// Workspace windows' outside outline width (styles.css :root).
const WB_OUTLINE_W_VAR = "--wb-outline-w";
const WB_OUTLINE_W_FALLBACK_PX = 1;
// Checkmark on a menu item (styles.css .tmb-dd-item.tmb-active).
const MENU_CHECK_CLASS = "tmb-active";
// Window-menu items for the Standings window's two views.
const STANDINGS_VIEW_ITEMS = [
  [".tmb-sys-standings", STANDINGS_VIEW.TABLE],
  [".tmb-sys-crosstable", STANDINGS_VIEW.CROSSTABLE],
];
// Unicode ellipsis is intentional: this glyph is rendered into the
// tournament-id span (user-facing), not a code token. ASCII-only rule
// does not apply to surfaced UI text.
const ID_ELLIPSIS = "…";
// Arena list sort keys: each is the tournament field it sorts on.
const SORT_BY = Object.freeze({
  NAME: "name", STATUS: "status", CREATED: "created_at", STARTED: "started_at",
});
const VALID_SORTS = new Set(Object.values(SORT_BY));
const statusCmp = rankCmp(STATUS_RANK);

function loadSortBy() {
  const saved = loadRaw(STORAGE_KEY.TOURNAMENTS_SORT_BY);
  return VALID_SORTS.has(saved) ? saved : SORT_BY.CREATED;
}

const IS_LOCAL = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);

// One builder for both action ribbons; prefix scopes the button classes
// (t-*, studio-*), withWorkspace / withSearch add Arena's workspace and
// search buttons. Search sits with New: both act on the list, not on the
// selected tournament.
export function ribbonHtml({ className, ariaLabel, prefix, withWorkspace, withSearch }) {
  const sep = '<span class="ribbon-sep" aria-hidden="true"></span>';
  const btn = (suffix, icon, title, { danger = false, enabled = false, iconClass = "" } = {}) => `
          <button class="ribbon-btn ${danger ? "ribbon-btn--danger " : ""}${prefix}-${suffix}"${enabled ? "" : " disabled"} aria-label="${title}" title="${title}">
            <wa-icon${iconClass ? ` class="${iconClass}"` : ""} name="${icon}"></wa-icon>
          </button>`;
  return `<div class="${className}" role="toolbar" aria-label="${ariaLabel}">
          ${btn("new", "plus", NEW_TOURNAMENT_LABEL, { enabled: true })}
          ${withSearch ? btn("search", "magnifying-glass", SEARCH_TOURNAMENTS_LABEL, { enabled: true }) : ""}
          ${sep}
          ${btn("start", "play", START_TOURNAMENT_LABEL, { iconClass: `${prefix}-start-icon` })}
          ${btn("stop", "hand", STOP_TOURNAMENT_LABEL)}
          ${sep}
          ${withWorkspace ? btn("workspace", "window-restore", OPEN_WORKSPACE_LABEL) : ""}
          ${btn("info", "circle-info", TOURNAMENT_DETAILS_LABEL)}
          ${btn("edit", "pen-to-square", EDIT_TOURNAMENT_LABEL)}
          ${btn("duplicate", "copy", COPY_TOURNAMENT_LABEL)}
          ${sep}
          ${btn("remove", "trash", DELETE_TOURNAMENT_LABEL, { danger: true })}
        </div>`;
}

const START_ICON_HTML = '<wa-icon class="t-start-icon" name="play"></wa-icon>';
const SPINNER_HTML = '<wa-spinner class="spinner-accent"></wa-spinner>';

// Swap a ribbon start button between its Start and Restart presentations.
export function setStartVerb(btn, isRestart) {
  const icon = btn.querySelector("wa-icon");
  if (icon) icon.setAttribute("name", isRestart ? "rotate-right" : "play");
  const label = isRestart ? RESTART_TOURNAMENT_LABEL : START_TOURNAMENT_LABEL;
  btn.setAttribute("aria-label", label);
  btn.setAttribute("title", label);
}

const PANEL_HTML = `
    <div class="tournaments-panel">
      <menu class="tournaments-menubar">
        <li class="tmb-menu tmb-sort-menu">
          <button class="tmb-item tmb-sort-btn">Sort</button>
          <ul class="tmb-dropdown">
            <li><button class="tmb-dd-item tmb-sort-opt" data-sort="${SORT_BY.NAME}">Name</button></li>
            <li><button class="tmb-dd-item tmb-sort-opt" data-sort="${SORT_BY.STATUS}">Status</button></li>
            <li><button class="tmb-dd-item tmb-sort-opt" data-sort="${SORT_BY.CREATED}">Created</button></li>
            <li><button class="tmb-dd-item tmb-sort-opt" data-sort="${SORT_BY.STARTED}">Started</button></li>
          </ul>
        </li>
        <li class="tmb-menu tmb-window-menu">
          <button class="tmb-item tmb-window-btn">Window</button>
          <ul class="tmb-dropdown">
            <li class="tmb-dd-submenu">
              <button class="tmb-dd-item tmb-sys-trigger">Tournament</button>
              <ul class="tmb-dropdown">
                <li><button class="tmb-dd-item tmb-sys-standings">Standings</button></li>
                <li><button class="tmb-dd-item tmb-sys-crosstable">Crosstable</button></li>
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
        ${ribbonHtml({ className: "tournaments-ribbon", ariaLabel: "Tournament actions", prefix: "t", withWorkspace: true, withSearch: true })}

        <div class="tournaments-body-main">
          <div class="tournaments-search-wrap inline-search">
            <wa-input class="tournaments-search" size="small" placeholder="${SEARCH_PLACEHOLDER}" clearable autocomplete="off"></wa-input>
          </div>
          <div class="tournaments-empty hidden">
            <p class="empty-message"></p>
          </div>

          <ul class="tournaments-list" role="listbox" tabindex="0"></ul>
        </div>
      </div>
    </div>
  `;

// ---- Generic async wrappers ---------------------------------------------

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
    ctx.ms.prune();
    syncRibbon(ctx);
    return;
  }
  ctx.newBtn.disabled = false;

  if (noTournaments) {
    ctx.emptyEl.classList.remove("hidden");
    ctx.emptyMsg.replaceChildren(...newTournamentCta(() => ctx.actions.create()));
    ctx.selectedId = null;
    ctx.ms.prune();
    syncRibbon(ctx);
    return;
  }
  ctx.emptyEl.classList.add("hidden");

  const sorted = sortedTournaments(ctx);
  // Over the whole list, not the filtered rows: filtering hides the
  // selection, it doesn't move it.
  if (!ctx.selectedId || !sorted.some((t) => t.id === ctx.selectedId)) {
    ctx.selectedId = (ctx.activeId && sorted.some((t) => t.id === ctx.activeId)) ? ctx.activeId : sorted[0].id;
  }
  const needle = ctx.filterText.trim().toLowerCase();
  const visible = sorted.filter((t) => matchesFilter(t, needle));
  for (const t of visible) {
    ctx.listEl.appendChild(renderRow(ctx, t));
  }
  if (!visible.length) ctx.listEl.appendChild(noMatchItem());
  ctx.ms.prune();
  syncRibbon(ctx);
  if (ctx.initialLoad) {
    ctx.initialLoad = false;
    maybeRestoreWorkspace(ctx);
  }
}

// The search filter: a case-insensitive substring of the tournament's name or
// of any of its engines' names. An empty needle matches everything.
function matchesFilter(t, needle) {
  return !needle || t.name.toLowerCase().includes(needle)
    || (t.engines || []).some((e) => e.name.toLowerCase().includes(needle));
}

function noMatchItem() {
  const li = document.createElement("li");
  li.className = "tournaments-no-match muted";
  li.textContent = NO_MATCH_MSG;
  return li;
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
    if (ctx.sortBy === SORT_BY.NAME) return dir * baseCompare(av, bv);
    if (ctx.sortBy === SORT_BY.STATUS) {
      const byRank = statusCmp(av, bv);
      if (byRank) return dir * byRank;
    }
    return dir * (av < bv ? -1 : 1);
  };
  return arr.sort(cmp);
}

function renderRow(ctx, t) {
  return renderTournamentRow(t, {
    selected: t.id === ctx.selectedId,
    onSelect: (t, ev) => {
      ctx.listEl.focus({ preventScroll: true });
      ctx.ms.handleClick(ev, t.id);
    },
    onInfo: (t) => ctx.actions.info(t),
  });
}

// Bind the list to a single row: collapse any multi-selection onto it, then
// rebind the panes (navigateTo repaints and syncs the ribbon itself).
function selectRow(ctx, id) {
  ctx.ms.collapseTo(id);
  if (ctx.selectedId === id) syncRibbon(ctx);
  else navigateTo(ctx, id);
}

function updateProgressInPlace(ctx, t) {
  if (t?.standings?.games == null) return;
  updateRowProgress(ctx.listEl.querySelector(`li[data-id="${t.id}"]`), t);
}

function selectedTournament(ctx) {
  return ctx.tournaments.find((t) => t.id === ctx.selectedId) || null;
}

// Tournaments the ribbon acts on: the whole marked set, or just the selected
// row when there is no multi-selection.
function selectedTournaments(ctx) {
  return selectionTargets(ctx.ms, ctx.tournaments, selectedTournament(ctx));
}

// Multi-selection ribbon: Remove is the only verb with a sensible bulk
// meaning. The server refuses to delete a running tournament, so a set
// holding it has no valid action at all.
function syncRibbonMulti(ctx) {
  for (const btn of [
    ctx.ribbonStartBtn, ctx.ribbonStopBtn, ctx.ribbonWorkspaceBtn,
    ctx.ribbonInfoBtn, ctx.ribbonEditBtn, ctx.ribbonDuplicateBtn,
  ]) {
    btn.disabled = true;
  }
  ctx.ribbonRemoveBtn.disabled = ctx.ms.ids().includes(ctx.activeId);
}

function syncRibbon(ctx) {
  syncTidyBtn(ctx);
  if (ctx.ms.multi()) {
    syncRibbonMulti(ctx);
    return;
  }
  const t = selectedTournament(ctx);
  if (!t) {
    ctx.ribbonStartBtn.disabled = true;
    ctx.ribbonStopBtn.disabled = true;
    ctx.ribbonWorkspaceBtn.disabled = true;
    ctx.ribbonInfoBtn.disabled = true;
    ctx.ribbonEditBtn.disabled = true;
    ctx.ribbonDuplicateBtn.disabled = true;
    ctx.ribbonRemoveBtn.disabled = true;
    if (!ctx.ribbonStartBtn.querySelector("wa-icon")) ctx.ribbonStartBtn.innerHTML = START_ICON_HTML;
    setStartVerb(ctx.ribbonStartBtn, false);
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
  // Duplicate always safe: it POSTs a fresh copy, never touches the source.
  ctx.ribbonDuplicateBtn.disabled = false;

  const starting = t.id === ctx.startingId;
  if (starting) {
    ctx.ribbonStartBtn.innerHTML = SPINNER_HTML;
  } else if (!ctx.ribbonStartBtn.querySelector("wa-icon")) {
    ctx.ribbonStartBtn.innerHTML = START_ICON_HTML;
  }
  setStartVerb(ctx.ribbonStartBtn, isRestart);
  const stopping = t.id === ctx.stoppingId;
  if (stopping) {
    ctx.ribbonStopBtn.disabled = true;
    ctx.ribbonStopBtn.innerHTML = SPINNER_HTML;
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

// Anchor the fixed ribbon's top to the menubar's rendered bottom so the rail
// starts at the menubar hairline (it extends to the viewport bottom in CSS).
function syncRibbonTop(ctx) {
  ctx.container.style.setProperty("--arena-ribbon-top", `${menubarBottom(ctx)}px`);
}

// Unrounded, as headerBottomPx: a rounded copy of a fractional bottom lands
// on a different device pixel at low DPR. 0 = the ribbon's CSS default.
function menubarBottom(ctx) {
  return ctx.container.querySelector(".tournaments-menubar")?.getBoundingClientRect().bottom ?? 0;
}

function openWorkspace(ctx, t) {
  const ribbon = ctx.container.querySelector(".tournaments-ribbon");
  // Reserve the ribbon's width on BOTH edges regardless of which side
  // it docks to (or whether it's floating). Keeps the workspace symmetric
  // and ribbon-side-flips don't reshape the available area.
  // Measure live so the reservation tracks the ribbon's true rendered width
  // and stays correct across resizes; zero width means the docked ribbon is
  // hidden (floating), so fall back to the var.
  const ribbonReserveW = () => {
    const w = ribbon?.getBoundingClientRect().width || 0;
    return w > 0 ? Math.ceil(w) : ribbonWidthPx(".tournaments-body");
  };
  // Windows draw their outline outside their box: inset by it so the outline
  // lands on the tournament list's side (and, in Clean, bottom) borders.
  const outlineW = () => cssVarPx(WB_OUTLINE_W_VAR, WB_OUTLINE_W_FALLBACK_PX);
  const top = menubarBottom(ctx);
  const left = ribbonReserveW() + outlineW();
  const getRight = () => Math.floor(window.innerWidth) - ribbonReserveW() - outlineW();
  // A hidden list (perspective switched away) has no box: no bound then, so
  // the workspace falls back to its own bottom edge.
  const getBottom = () => {
    const r = ctx.listEl.getBoundingClientRect();
    return r.height ? r.bottom - outlineW() : Infinity;
  };
  openTournamentWorkspace({ api: ctx.api, events: ctx.events, log: ctx.log, tournament: t, top, left, getRight, getBottom });
  // Seed the workspace's view of the other-active tournament so the
  // banner Restart button reflects busy state on open, not just after
  // the next loadList tick.
  syncWorkspaceOtherActive(ctx);
  syncWindowMenu(ctx);
  syncRibbon(ctx);
}

// ---- Verbs --------------------------------------------------------------

async function startOne(ctx, t) {
  // The gate owns the confirms: wipe confirm for stopped/failed, or the
  // engine-drift dialog when Settings > Engines changed since the freeze.
  let started = false;
  try {
    started = await gatedStart({ api: ctx.api, log: ctx.log }, t);
  } catch (e) {
    reportError({ log: ctx.log }, `Starting "${t.name}" failed`, e);
    // Resync even on failure: Update & start may have PATCHed (reset to
    // idle, games wiped) before the start POST failed.
    await ctx.loadList();
    return;
  }
  if (!started) return;
  await ctx.loadList();
}

async function stopOne(ctx, t) {
  // Stop is destructive -- on next Start the tournament is restarted
  // from scratch. Warn before clicking through.
  const games = t.standings?.games ?? 0;
  const message = games > 0
    ? `Stop "${t.name}"?\nRestarting discards all ${games} recorded game${games === 1 ? "" : "s"}.`
    : `Stop "${t.name}"?\nOn restart this tournament will start from scratch.`;
  const ok = await confirm({
    message,
    okLabel: "Stop",
    destructive: true,
    messageClass: CONFIRM_MULTILINE_CLASS,
  });
  if (!ok) return;
  try {
    await ctx.api("POST", `/api/tournaments/${t.id}/stop`);
  } catch (e) {
    reportError({ log: ctx.log }, `Stopping "${t.name}" failed`, e);
  }
  await ctx.loadList();
}

async function deleteTournament(ctx, t) {
  await ctx.api("DELETE", `/api/tournaments/${t.id}`);
  if (getActiveWorkspace()?.tournamentId === t.id) getActiveWorkspace().close();
  clearWorkspaceState(t.id);
}

async function removeOne(ctx, t) {
  const ok = await confirm({
    message: `Remove "${t.name}"?\n${DELETE_WARNING}`,
    okLabel: REMOVE_ACTION_LABEL,
    destructive: true,
    messageClass: CONFIRM_MULTILINE_CLASS,
  });
  if (!ok) return;
  try {
    await deleteTournament(ctx, t);
    toast(`Removed tournament "${t.name}"`, { variant: "success" });
  } catch (e) {
    reportError({ log: ctx.log }, `Removing "${t.name}" failed`, e);
    return;
  }
  await ctx.loadList();
}

// Bulk delete for a multi-selection (always 2+ rows -- the ribbon routes a
// single selection to removeOne). Deletes are sequential so one failure
// doesn't abort the rest; the survivors reappear on the reload.
async function removeMany(ctx, list) {
  const n = list.length;
  const ok = await confirm({
    message: `Remove ${n} tournaments?\n${DELETE_WARNING}`,
    okLabel: `${REMOVE_ACTION_LABEL} ${n}`,
    destructive: true,
    messageClass: CONFIRM_MULTILINE_CLASS,
  });
  if (!ok) return;
  const failed = [];
  for (const t of list) {
    try {
      await deleteTournament(ctx, t);
    } catch (e) {
      failed.push({ t, e });
    }
  }
  const removed = n - failed.length;
  if (removed) toast(`Removed ${removed} tournament${removed === 1 ? "" : "s"}`, { variant: "success" });
  if (failed.length) {
    const { t, e } = failed[0];
    const more = failed.length > 1 ? ` (+${failed.length - 1} more)` : "";
    reportError({ log: ctx.log }, `Removing "${t.name}"${more} failed`, e);
  }
  await ctx.loadList();
}

// Ribbon Remove for either perspective: a multi-selection deletes in bulk,
// anything else falls through to the single-tournament confirm.
export function removeSelected(actions, list) {
  if (list.length > 1) actions.removeMany(list);
  else if (list.length === 1) actions.remove(list[0]);
}

// Shared tournament verbs for other UIs (e.g. Studio). The action functions
// only read {api, log, settings, loadList} off ctx, so a minimal ctx adapter
// lets a different perspective reuse them with no change to the verbs.
export function tournamentActions({ api, log, getSettings, reload, onStatusClick }) {
  const ctx = {
    api, log, loadList: reload, onStatusClick,
    get settings() { return getSettings ? getSettings() : null; },
  };
  return guardedVerbs(ctx);
}

// One guard()ed wrapper per tournament verb. Every verb awaits network or a
// confirm before acting, so an unguarded double-click stacks dialogs or
// doubles POSTs. Shared by Arena's ribbon and the Studio adapter above.
function guardedVerbs(ctx) {
  return {
    create: guard(() => openNewTournamentDialog(ctx)),
    edit: guard((t) => openEditTournamentDialog(ctx, t)),
    duplicate: guard((t) => openDuplicateTournamentDialog(ctx, t)),
    info: guard((t) => openInfoDialog(ctx, t)),
    start: guard((t) => startOne(ctx, t)),
    stop: guard((t) => stopOne(ctx, t)),
    remove: guard((t) => removeOne(ctx, t)),
    removeMany: guard((list) => removeMany(ctx, list)),
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
      // onStatusClick (Studio only): renders the Games tally as a link that
      // closes the dialog and jumps to Standings (only when games exist).
      // Absent in Arena, where Standings has no unambiguous deep-link target.
      const onStatus = ctx.onStatusClick
        ? () => { resolve(); ctx.onStatusClick(detailed); }
        : null;
      wrap.appendChild(buildInfoContent(ctx, detailed, onStatus));
      dialog.appendChild(wrap);
    },
  });
}

function buildInfoContent(ctx, t, onStatusClick) {
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
      btn.addEventListener("click", cooldown(async () => {
        try {
          await ctx.api("POST", `/api/tournaments/${t.id}/reveal`);
        } catch (e) {
          reportError({ log: ctx.log }, "Could not open folder", e);
        }
      }, REVEAL_COOLDOWN_MS));
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
  // Studio-only: link the games tally to Standings, but only once games
  // exist (idle, or stopped/failed before any played, has none to show).
  const games = formatGames(t);
  const hasGames = (t.standings?.games ?? 0) > 0;
  if (onStatusClick && hasGames) {
    const link = document.createElement("button");
    link.type = "button";
    link.className = "tournament-info-games-link";
    link.textContent = games;
    link.title = SHOW_STANDINGS_TITLE;
    link.addEventListener("click", onStatusClick);
    row("Games", link);
  } else {
    row("Games", games);
  }
  if (tpl.tournament_type === "gauntlet") row("Seeds", tpl.seeds);
  row("Ponder", tpl.ponder ? "On" : "Off");
  row("CPU affinity", tpl.pin_affinity ? "Pinned" : "Off");
  row("Resign", formatResign(tpl.resign) ?? "Off");
  row("Draw adjudication", formatDraw(tpl.draw) ?? "Off");
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
  // basename() of an unset path is "", which defaultSpan shows as the default.
  row("Syzygy", defaultSpan(basename(ed.syzygy_path), ed.syzygy_path));
  row("Opening book", defaultSpan(basename(ed.book_path), ed.book_path));
  if (ed.book_path) {
    row("Book plies", ed.book_plies);
    row("Book order", ed.book_order);
  }
  row("Created", formatStamp(t.created_at));
  if (t.status === STATUS.RUNNING || t.status === STATUS.FAILED || t.status === STATUS.STOPPED) row("Started", formatStamp(t.started_at));
  if (t.status === STATUS.STOPPED) row("Stopped", formatStamp(t.stopped_at));

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

// Effective book of the settings chain, each field resolved independently:
// tournament default_template values win, absent ones fall through to the
// Common defaults. The New dialog's book INHERITs this; null = no book
// anywhere up the chain (or the default_template explicitly turned it off).
function effectiveSettingsBook(saved) {
  const tpl = saved.default_template || {};
  const path = "book_path" in tpl ? tpl.book_path : saved.engine_default_book_path;
  if (!path) return null;
  return {
    path,
    plies: tpl.book_plies ?? saved.engine_default_book_plies,
    order: tpl.book_order ?? saved.engine_default_book_order,
  };
}

// Prepare the form's book state in `defaults` (mutated) and return the
// inherited book for the tri-state X-cycle. Edit/Duplicate (initialBook = the
// frozen engine_defaults) rewrite the template book keys from the snapshot: a
// set book prefills and doubles as the restore target; an explicitly book-less
// snapshot pins OFF (nothing to restore). A legacy snapshot without book keys
// -- like a fresh create (no initialBook) -- leaves the keys absent so the
// form INHERITs the settings chain.
function prepareBookMount(defaults, initialBook, saved) {
  for (const k of BOOK_KEYS) delete defaults[k];
  if (initialBook && "book_path" in initialBook) {
    if (!initialBook.book_path) {
      defaults.book_path = "";
      return null;
    }
    defaults.book_path = initialBook.book_path;
    if (initialBook.book_plies != null) defaults.book_plies = initialBook.book_plies;
    if (initialBook.book_order) defaults.book_order = initialBook.book_order;
    return {
      path: initialBook.book_path,
      plies: initialBook.book_plies,
      order: initialBook.book_order,
    };
  }
  return effectiveSettingsBook(saved);
}

// Shared dialog body for both create and edit flows.
// Returns a Promise that resolves to {name, template, engines} or null.
async function openTournamentDialog(ctx, { label, actionLabel, initialName, initialEngines, initialTemplate, initialBook, available, onSubmit, onOpened }) {
  // Read the saved defaults fresh from the server store here, not from a
  // per-view settings cache: Arena and Studio refresh that cache differently
  // (Studio not at all), so the shared dialog must own its source of truth.
  // Edit passes initialTemplate (the frozen template), which still wins.
  const saved = await ctx.api("GET", "/api/tournament-settings").catch(() => ({}));
  const defaults = {
    ...(initialTemplate ||
      saved.default_template ||
      { tc: "10+0.1", rounds: 10, games_in_parallel: 1 }),
  };
  const inheritedBook = prepareBookMount(defaults, initialBook, saved);
  const sprtDefaults = saved.sprt_defaults;
  const pathRow = makePathRow(ctx.api);

  return showDialog({
    label,
    width: "min(660px, 94vw)",
    // Tall enough that the body fits the form with a section expanded (the
    // ttf-sections area reserves a fixed height), so nothing scrolls or shifts.
    height: "min(780px, 92vh)",
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
        pathRow,
        inheritedBook,
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
      tplCtl.setMaxSeeds(builder.getEngines().length);
      nameInput.addEventListener("input", refreshValidity);
      builder.onChange(() => {
        sprtCtl.setAvailable(builder.getEngines().length === 2);
        tplCtl.setMaxSeeds(builder.getEngines().length);
        refreshValidity();
      });

      actionBtn.addEventListener("click", guard(async () => {
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
        const globalDefaults = await loadGlobalEngineDefaults(ctx.api);
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
      }));

      dialog.append(wrap, actionBtn);
      requestAnimationFrame(() => nameInput.focus());
      // Fire after the dialog is on the top layer so a warning toast layers
      // above the modal scrim instead of behind it (wa-after-show, not the
      // body rAF, which runs before showDialog flips dialog.open).
      if (onOpened) {
        const onShow = (ev) => {
          if (ev.target !== dialog) return;
          dialog.removeEventListener("wa-after-show", onShow);
          onOpened();
        };
        dialog.addEventListener("wa-after-show", onShow);
      }
    },
  });
}

// Registry engines for the New/Edit/Copy dialogs, or null (after telling the
// user why) when the registry fails to load or holds fewer than 2 engines.
async function loadAvailableEngines(ctx) {
  let registry;
  try {
    registry = await ctx.api("GET", "/engines");
  } catch (e) {
    reportError({ log: ctx.log }, "Loading engine registry failed", e);
    return null;
  }
  const available = registry.engines || [];
  if (available.length < 2) {
    toast(buildToastWithActions(NEED_TWO_ENGINES_MSG, [OPEN_ENGINES_ACTION]), { variant: "danger" });
    return null;
  }
  return available;
}

// New and Copy both POST a fresh tournament. false keeps the dialog open; a
// name-taken failure rethrows flagged so the dialog shakes the name field.
async function postTournament(ctx, data, failMsg) {
  try {
    await ctx.api("POST", "/api/tournaments", data);
    toast(`Created new tournament "${data.name}"`, { variant: "success" });
  } catch (e) {
    reportError({ log: ctx.log }, failMsg, e);
    if (/-> 409\b/.test(e.message)) throw Object.assign(e, { isNameCollision: true });
    return false;
  }
  await ctx.loadList();
}

async function openNewTournamentDialog(ctx) {
  const available = await loadAvailableEngines(ctx);
  if (!available) return;

  await openTournamentDialog(ctx, {
    label: NEW_TOURNAMENT_LABEL,
    actionLabel: CREATE_ACTION_LABEL,
    initialName: "",
    initialEngines: [],
    // No initialTemplate: New has no frozen template. The dialog prefills
    // from the saved default_template it reads fresh from the server, so the
    // popup never depends on a per-view settings cache (Edit passes the
    // existing tournament's frozen template, which does win).
    initialTemplate: null,
    available,
    onSubmit: (data) => postTournament(ctx, data, "Creating tournament failed"),
  });
}

// Resolve a tournament's engines to registry entries so the builder can
// preselect them. Prefer id match; fall back to name then cmd. Returns the
// count of engines no longer in the registry so the caller can warn -- the
// warning is toasted after the dialog opens so it isn't dimmed by the scrim.
function resolveInitialEngines(available, engines) {
  const match = buildEngineMatcher(available);
  const initialEngines = [];
  let droppedCount = 0;
  for (const e of engines || []) {
    const entry = match(e);
    if (entry) initialEngines.push(entry);
    else droppedCount += 1;
  }
  return { initialEngines, droppedCount };
}

// Warn about engines dropped during resolve. Fired from inside the dialog
// body so the toast layers above the modal scrim instead of behind it.
function warnDroppedEngines(droppedCount, verb) {
  if (droppedCount <= 0) return;
  toast(
    `${droppedCount} engine${droppedCount === 1 ? "" : "s"} no longer in the registry -- re-add before ${verb}.`,
    { variant: "warning", duration: TOAST_DURATION_MS },
  );
}

// Suggest a non-conflicting copy name: "Foo (copy)", then "Foo (copy 2)"...
function suggestCopyName(base, existingNames) {
  const taken = new Set(existingNames);
  let candidate = `${base}${COPY_SUFFIX}`;
  for (let n = 2; taken.has(candidate); n += 1) candidate = `${base}${COPY_SUFFIX} ${n}`;
  return candidate;
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

  const available = await loadAvailableEngines(ctx);
  if (!available) return;

  const { initialEngines, droppedCount } = resolveInitialEngines(available, t.engines);

  await openTournamentDialog(ctx, {
    label: EDIT_TOURNAMENT_LABEL,
    actionLabel: "Apply",
    initialName: t.name,
    initialEngines,
    initialTemplate: t.template || null,
    initialBook: t.engine_defaults || null,
    available,
    onOpened: () => warnDroppedEngines(droppedCount, "applying"),
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

// Duplicate: prefill the create dialog from an existing tournament (engines +
// frozen template, SPRT included) under a non-conflicting copy name. Unlike
// Edit this POSTs a new tournament, so there are no game-deletion gates.
async function openDuplicateTournamentDialog(ctx, t) {
  const available = await loadAvailableEngines(ctx);
  if (!available) return;

  const { initialEngines, droppedCount } = resolveInitialEngines(available, t.engines);

  // Existing names for a conflict-free default: use the loaded list when
  // present (Arena), else fetch (Studio's action ctx carries no list). On
  // fetch failure fall back to [] -- the suggestion may then collide, in
  // which case the POST's 409 shake is the backstop.
  let existing = ctx.tournaments;
  if (!existing) {
    const list = await ctx.api("GET", "/api/tournaments").catch(() => null);
    existing = list?.tournaments || [];
  }
  const initialName = suggestCopyName(t.name, existing.map((x) => x.name));

  await openTournamentDialog(ctx, {
    label: COPY_TOURNAMENT_LABEL,
    actionLabel: CREATE_ACTION_LABEL,
    initialName,
    initialEngines,
    initialTemplate: t.template || null,
    initialBook: t.engine_defaults || null,
    available,
    onOpened: () => warnDroppedEngines(droppedCount, "creating the copy"),
    onSubmit: (data) => postTournament(ctx, data, "Duplicating tournament failed"),
  });
}

// ---- Window / sort menus ------------------------------------------------

function syncWindowMenu(ctx) {
  ctx.windowMenuBtn.disabled = !getActiveWorkspace();
}

// Closes every open menu and submenu. Focus inside a closing menu (keyboard
// use) returns to its menubar button instead of dropping to <body>.
function closeMenus(ctx) {
  for (const m of ctx.container.querySelectorAll(`.tmb-menu.${MENU_OPEN_CLASS}, .tmb-dd-submenu.${MENU_OPEN_CLASS}`)) {
    const hadFocus = m.contains(document.activeElement);
    m.classList.remove(MENU_OPEN_CLASS);
    if (hadFocus && m.classList.contains("tmb-menu")) m.querySelector(":scope > .tmb-item")?.focus();
  }
}

// Toggle a menubar menu; opening moves focus to its first item.
function toggleMenu(ctx, menu) {
  const isOpen = menu.classList.contains(MENU_OPEN_CLASS);
  closeMenus(ctx);
  if (isOpen) return;
  menu.classList.add(MENU_OPEN_CLASS);
  focusFirstItem(menu.querySelector(":scope > .tmb-dropdown"));
}

function syncSortMenu(ctx) {
  for (const opt of ctx.container.querySelectorAll(".tmb-sort-opt")) {
    const active = opt.dataset.sort === ctx.sortBy;
    opt.classList.toggle("is-active", active);
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
  scrollSortedRowIntoView(ctx.listEl, ".tournament-row.selected");
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

// Window-menu layout toggles: [button, layout, apply it to a workspace].
function layoutToggles(ctx) {
  return [
    [ctx.snapBtn, LAYOUT.SNAP, (ws) => ws.snap()],
    [ctx.tileBtn, LAYOUT.TILE, (ws) => ws.tile()],
    [ctx.tidyBtn, LAYOUT.TIDY, (ws) => ws.tidy()],
  ];
}

function syncTidyBtn(ctx) {
  const layout = getActiveLayout();
  for (const [btn, mode] of layoutToggles(ctx)) btn.classList.toggle(MENU_CHECK_CLASS, layout === mode);
}

// Check the open Standings window's view; synced on menu open, since the
// window's own title-bar toggle can flip it too.
function syncStandingsViewItems(ctx) {
  const view = getActiveWorkspace()?.standingsView;
  for (const [cls, v] of STANDINGS_VIEW_ITEMS) {
    ctx.container.querySelector(cls).classList.toggle(MENU_CHECK_CLASS, view === v);
  }
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
    if (t) ctx.actions.info(t);
  });
  ctx.ribbonEditBtn.addEventListener("click", () => {
    const t = selectedTournament(ctx);
    if (t && !ctx.ribbonEditBtn.disabled) ctx.actions.edit(t);
  });
  ctx.ribbonDuplicateBtn.addEventListener("click", () => {
    const t = selectedTournament(ctx);
    if (t && !ctx.ribbonDuplicateBtn.disabled) ctx.actions.duplicate(t);
  });
  ctx.ribbonRemoveBtn.addEventListener("click", () => {
    if (ctx.ribbonRemoveBtn.disabled) return;
    removeSelected(ctx.actions, selectedTournaments(ctx));
  });
}

// Search (inline-search.js): a bar that slides down under the menubar (not up
// from the bottom as on the Engines page: the ribbon runs down the left edge,
// and the bottom is where workspace windows go). Clicks on the list don't close
// it, so a filtered row can be picked. Workspace windows would hide the list,
// so opening minimizes them (with a toast saying so) and closing restores them;
// a click on the toast is a click elsewhere. Returns the close, which unmount
// calls so windows don't stay minimized.
function setupTournamentSearch(ctx) {
  // Windows this search minimized, and its toast's dismiss.
  let minimizedWbs = [];
  let dismissToast = null;

  function minimizeWorkspace() {
    const ws = getActiveWorkspace();
    if (!ws || ws.isHidden()) return;
    minimizedWbs = ws.minimizeAll();
    if (!minimizedWbs.length) return;
    const msg = document.createElement("span");
    msg.className = "toast-sort-msg";
    const restoreBtn = document.createElement("button");
    restoreBtn.className = "toast-action-btn toast-ws-toggle toast-ws-restore";
    restoreBtn.setAttribute("aria-label", RESTORE_WINDOWS_LABEL);
    restoreBtn.title = RESTORE_WINDOWS_LABEL;
    msg.append(SEARCH_MINIMIZED_MSG, restoreBtn);
    dismissToast = toast(msg, { duration: 0 });
  }

  function restoreWorkspace() {
    dismissToast?.();
    dismissToast = null;
    getActiveWorkspace()?.restoreWindows(minimizedWbs);
    minimizedWbs = [];
  }

  const search = wireInlineSearch({
    btn: ctx.container.querySelector(".t-search"),
    bar: ctx.container.querySelector(".tournaments-search-wrap"),
    input: ctx.container.querySelector(".tournaments-search"),
    inside: [ctx.listEl],
    onInput: (text) => {
      ctx.filterText = text;
      renderList(ctx);
    },
    onOpen: minimizeWorkspace,
    onClose: () => {
      ctx.filterText = "";
      renderList(ctx);
      // The full list is back: keep the selection in view.
      scrollSortedRowIntoView(ctx.listEl, SELECTED_ROW_SEL);
      restoreWorkspace();
    },
  });
  return search.close;
}

function wireListKeyboard(ctx) {
  wireArrowKeyNav(ctx.listEl, {
    rows: ".tournament-row",
    selected: SELECTED_ROW_SEL,
    lead: () => ctx.listEl.querySelector(`.tournament-row[data-id="${ctx.ms.lead()}"]`),
    select: (row) => selectRow(ctx, row.dataset.id),
    extend: (row) => ctx.ms.extendTo(row.dataset.id),
  });
}

function wireMenus(ctx) {
  for (const menu of [ctx.sortMenu, ctx.windowMenu]) {
    attachMenuNav(menu.querySelector(":scope > .tmb-dropdown"), { close: () => closeMenus(ctx) });
  }
  ctx.sortMenuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleMenu(ctx, ctx.sortMenu);
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
    syncStandingsViewItems(ctx);
    toggleMenu(ctx, ctx.windowMenu);
  });

  // Each toggle applies its layout, or reverts when that layout is active.
  for (const [btn, mode, apply] of layoutToggles(ctx)) {
    btn.addEventListener("click", () => {
      closeMenus(ctx);
      const ws = getActiveWorkspace();
      if (!ws) return;
      if (getActiveLayout() === mode) ws.untidy(); else apply(ws);
      syncTidyBtn(ctx);
    });
  }
  ctx.container.querySelector(".tmb-closeall").addEventListener("click", () => {
    closeMenus(ctx);
    const ws = getActiveWorkspace();
    if (ws) { dismissSortToastNow(ctx); ws.closeAll(); }
    syncWindowMenu(ctx);
  });
  for (const [cls, open] of [
    ...STANDINGS_VIEW_ITEMS.map(([c, view]) => [c, (ws) => ws.showStandingsView(view)]),
    [".tmb-sys-schedule",   (ws) => ws.openSystemWindow("schedule")],
    [".tmb-sys-engines",    (ws) => ws.openSystemWindow("engines")],
    [".tmb-sys-log",        (ws) => ws.openSystemWindow("log")],
  ]) {
    ctx.container.querySelector(cls).addEventListener("click", () => {
      closeMenus(ctx);
      const ws = getActiveWorkspace();
      if (ws) open(ws);
    });
  }
}

// oversized-ok removed: factory decomposed into module-level helpers below.
export function mountTournaments({ container, api, events, log }) {
  container.innerHTML = PANEL_HTML;

  const ctx = {
    container, api, events, log,
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
    ribbonDuplicateBtn: container.querySelector(".t-duplicate"),
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
    sortBy: loadSortBy(),
    sortAsc: loadRaw(STORAGE_KEY.TOURNAMENTS_SORT_ASC) === "true",
    // Search filter text (setupTournamentSearch); "" shows every row.
    filterText: "",

    // Persistent sort toast (reused in place to avoid flicker on re-sort).
    dismissSortToast: null,
    sortToastTextEl: null,
    sortToastToggleBtn: null,
    sortToastHidden: false,
    sortToastHiddenWbs: [],

    tournamentsTabActive: false,
  };
  markSelectable(ctx.listEl, { rows: ".tournament-row" });
  suppressMultiClickSelect(ctx.listEl);
  suppressModifierClickSelect(ctx.listEl);
  ctx.ms = createMultiSelect({
    getRows: () => ctx.listEl.querySelectorAll(".tournament-row"),
    getAnchorId: () => ctx.selectedId,
    selectOne: (id) => selectRow(ctx, id),
    onChange: () => syncRibbon(ctx),
  });

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
  ctx.actions = guardedVerbs(ctx);

  wireRibbon(ctx);
  const ribbonOverflow = attachRibbonOverflow(container.querySelector(".tournaments-ribbon"));
  const stopSearch = setupTournamentSearch(ctx);
  wireListKeyboard(ctx);
  wireMenus(ctx);
  ctx.newBtn.addEventListener("click", () => ctx.actions.create());
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
  window.addEventListener(APP_EVT.TOURNAMENTS_ROOT_CHANGED, ctx.loadList);

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

  // Keep the ribbon's top pinned to the menubar bottom across resizes.
  ctx.onRibbonResize = () => syncRibbonTop(ctx);
  window.addEventListener("resize", ctx.onRibbonResize);
  requestAnimationFrame(() => syncRibbonTop(ctx));

  return {
    dismissSortToast: () => dismissSortToastNow(ctx),
    restoreWorkspace: () => restoreWorkspace(ctx),
    unmount() {
      offEvents();
      window.clearInterval(pollIntervalId);
      window.removeEventListener(APP_EVT.SETTINGS_CHANGED, ctx.onSettingsChanged);
      window.removeEventListener(APP_EVT.WORKSPACE_CLOSED, ctx.onWorkspaceClosed);
      window.removeEventListener(APP_EVT.TOURNAMENTS_ROOT_CHANGED, ctx.loadList);
      mqMobile.removeEventListener("change", ctx.onViewportChange);
      mqMobileH.removeEventListener("change", ctx.onViewportChange);
      mqMobileHPlay.removeEventListener("change", ctx.onViewportChange);
      window.removeEventListener("resize", ctx.onRibbonResize);
      document.removeEventListener("click", ctx.onMenuDocClick);
      stopSearch();
      ribbonOverflow.detach();
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

  // One pane row: click (multi-)selects within its pane, dblclick moves it
  // across. The tooltip shows a name cut off by the pane width.
  function paneItem(e, { selected, anchor, idsInOrder, onMove }) {
    const li = document.createElement("li");
    li.className = "ne-item" + (selected.has(e.id) ? " selected" : "");
    li.dataset.id = e.id;
    li.textContent = e.name;
    li.title = e.name;
    li.addEventListener("click", (ev) => {
      applyListClick(ev, e.id, selected, anchor, idsInOrder);
      render();
    });
    li.addEventListener("dblclick", () => onMove([e.id]));
    return li;
  }

  function render() {
    availableList.replaceChildren(...available
      .filter((e) => !pickedIds.includes(e.id))
      .map((e) => paneItem(e, {
        selected: availableSelected, anchor: availableAnchor, idsInOrder: visibleAvailableIds, onMove: doAdd,
      })));
    pickedList.replaceChildren(...pickedIds
      .map((id) => byId.get(id))
      .filter(Boolean)
      .map((e) => paneItem(e, {
        selected: pickedSelected, anchor: pickedAnchor, idsInOrder: () => [...pickedIds], onMove: doRemove,
      })));

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
    suppressModifierClickSelect(list);
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
      return pickedIds.map((id) => engineRefFromRegistry(byId.get(id)));
    },
    getPickedRegistry() {
      // Full registry entries (with options + option_schema) for the
      // picked engines -- used by the rescheck resolver.
      return pickedIds.map((id) => byId.get(id)).filter(Boolean);
    },
    onChange(fn) { listeners.add(fn); },
  };
}
