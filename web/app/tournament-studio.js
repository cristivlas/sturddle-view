// Tournament Studio: experimental UX for the Tournaments perspective.
// The classic windowed workspace ("Arena") lives in tournament-workspace.js;
// the TOURNAMENT_UX setting (Display tab) picks which one mounts.
//
// Layout: a left action ribbon + matching right gutter frame a centered
// column. The column stacks a selected-tourney header over a resizable
// split -- Boards (live boards) on top, a two-pane tabbed row below. The
// bottom row splits left (Engine Instances / Live Games) from a wider right
// (Tourneys / Standings / Event Log). Both splits are drag-resizable.

import { APP_EVT } from "./app-events.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadJson, loadRaw, saveJson, saveRaw } from "./storage.js";
import { progressBarHtml, progressLabelHtml, sprtBadgeHtml, statusBadgeHtml, totalGames } from "./tournament-row.js";
import { SORT_DIR } from "./col-sort.js";
import { attachLayeredSort, sortByStack } from "./sort-stack.js";
import { attachColumnResize, makePctApplySizes } from "./col-resize.js";
import { reportError, toast } from "./dialogs.js";
import { copyRowsAsLines, debounce, escapeHtml, selectContentsOnCtrlA } from "./wb-utils.js";
import { crashErrorLine, CRASH_TOAST_DURATION_MS, EVT, EVT_PREFIX, KIND, STATUS } from "./tournament-events.js";
import { newTournamentCta, tournamentActions } from "./tournaments.js";
import { RESULT, SIDE } from "./chess-consts.js";
import { addLogEntry, applyEventKind, createLiveState, seedFromDetail } from "./tournament-live-state.js";
import { closeAllLiveGames, getLiveWindows, isLiveWindowOpen, LIVE_MIN_HEIGHT, LIVE_MIN_WIDTH, openFrozenGameWindow, openLiveGameWindow, replayTournamentGame } from "./tournament-live-game.js";
import { makeStandingsBody, renderStandings } from "./tournament-standings.js";
import { makeH2HBody, renderH2H } from "./tournament-h2h.js";
import { renderEventLogList } from "./tournament-eventlog.js";
import { renderInfoWall } from "./tournament-info.js";
import { mqMobile } from "./breakpoints.js";

const TOURNAMENTS_ENDPOINT = "/api/tournaments";
const TOURNAMENT_SETTINGS_ENDPOINT = "/api/tournament-settings";
const SETTINGS_ENDPOINT = "/settings";
const LOAD_FAIL_MSG = "Loading tournaments failed";
const NO_ENGINES_MSG = "No active engines.";
const NO_GAMES_MSG = "No games in play.";
const REVIEW_FAIL_MSG = "Review failed";
// Coalesce bursty WS events into one list reload.
const LIST_RELOAD_DEBOUNCE_MS = 150;
// Coalesce standings re-fetches while the selected tourney is running.
const STANDINGS_REFRESH_DEBOUNCE_MS = 400;
// Safety cap on the perspective reveal gate: reveal anyway if the list/boards
// haven't signalled ready by now (a stuck fetch must not hide the UI forever).
const READY_TIMEOUT_MS = 4000;

// Boards region grid: always 4 columns; cells stretch to fill the region
// width (keeping the board square), clamped to the Arena minimum board
// width -- below that the row overflows and scrolls. Unlimited rows scroll
// vertically. Boards are laid out (no-move), absolutely positioned.
const STUDIO_COLS = 4;
const STUDIO_BOARD_GAP = 1;
// Inset so boards don't sit flush against the region border (abs-positioned
// boards ignore container padding, so the offset is applied in placement).
const STUDIO_BOARD_PAD = 1;
const STUDIO_BOARD_CLASS = "sturddle-wb-studio no-move no-resize";
const BOARD_RESIZE_DEBOUNCE_MS = 120;
// Default active tab per bottom group (first tab) when none is remembered.
const STUDIO_TAB_DEFAULT_LEFT = "livegames";
const STUDIO_TAB_DEFAULT_RIGHT = "tourneys";
// Tourney table default column widths (Status, Created, Name, Completed) + resize floor.
const STUDIO_TOURNEY_DEFAULT_PCTS = [12, 22, 16, 50];
const STUDIO_TOURNEY_MIN_PCT = 10;
// History table default column widths (#, White, Black, Result, Opening) + resize floor.
const STUDIO_HISTORY_DEFAULT_PCTS = [5, 20, 20, 15, 40];
const STUDIO_HISTORY_MIN_PCT = 5;

export const TOURNAMENT_UX = Object.freeze({ ARENA: "arena", STUDIO: "studio" });

// Top-nav tab label per UX mode.
const UX_LABEL = Object.freeze({ [TOURNAMENT_UX.ARENA]: "Arena", [TOURNAMENT_UX.STUDIO]: "Studio" });

// Splitter drag axis: vertical drag resizes heights, horizontal resizes widths.
const AXIS = Object.freeze({ Y: "y", X: "x" });
// Fallback flex-grow when a pane has none computed yet.
const DEFAULT_SPLIT_GROW = 1;

// Label for the currently selected UX mode (top-nav tab text).
export function tournamentUxLabel() {
  return UX_LABEL[getTournamentUx()];
}

// Read the persisted UX mode; anything unrecognized falls back to Studio.
export function getTournamentUx() {
  const v = loadRaw(STORAGE_KEY.TOURNAMENT_UX);
  return v === TOURNAMENT_UX.ARENA ? TOURNAMENT_UX.ARENA : TOURNAMENT_UX.STUDIO;
}

// ---- Studio shell --------------------------------------------------------

const STUDIO_HTML = `
  <div class="studio-panel">
    <div class="studio-body">
      <div class="studio-ribbon" role="toolbar" aria-label="Studio actions">
        <button class="ribbon-btn studio-new" aria-label="New tournament" title="New tournament">
          <wa-icon name="plus"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn studio-start" disabled aria-label="Start" title="Start">
          <wa-icon name="play"></wa-icon>
        </button>
        <button class="ribbon-btn studio-stop" disabled aria-label="Stop" title="Stop">
          <wa-icon name="hand"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn studio-info" disabled aria-label="Info" title="Info">
          <wa-icon name="circle-info"></wa-icon>
        </button>
        <button class="ribbon-btn studio-edit" disabled aria-label="Edit" title="Edit">
          <wa-icon name="pen-to-square"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn ribbon-btn--danger studio-remove" disabled aria-label="Remove" title="Remove">
          <wa-icon name="trash"></wa-icon>
        </button>
      </div>

      <div class="studio-main">
        <div class="studio-boards"><div class="studio-boards-wall" hidden></div><div class="studio-boards-canvas"></div></div>
        <div class="studio-boards-tray" hidden></div>
        <div class="studio-grip-row" role="separator" aria-orientation="horizontal"></div>
        <div class="studio-bottom">
          <div class="studio-bottom-left">
            <wa-tab-group class="studio-tabs">
              <wa-tab panel="livegames">Playing</wa-tab>
              <wa-tab panel="engines">Engines</wa-tab>
              <wa-tab-panel name="engines"><div class="studio-pane studio-pane-engines"></div></wa-tab-panel>
              <wa-tab-panel name="livegames"><div class="studio-pane studio-pane-livegames"></div></wa-tab-panel>
            </wa-tab-group>
          </div>
          <div class="studio-grip-col" role="separator" aria-orientation="vertical"></div>
          <div class="studio-bottom-right">
            <wa-tab-group class="studio-tabs">
              <wa-tab panel="tourneys">Tourneys</wa-tab>
              <wa-tab panel="standings">Standings</wa-tab>
              <wa-tab panel="h2h">Head-to-Head</wa-tab>
              <wa-tab panel="history">Games</wa-tab>
              <wa-tab panel="log">Event Log</wa-tab>
              <wa-tab-panel name="tourneys"><div class="studio-pane studio-pane-tourneys"></div></wa-tab-panel>
              <wa-tab-panel name="standings"><div class="studio-pane studio-pane-standings"></div></wa-tab-panel>
              <wa-tab-panel name="h2h"><div class="studio-pane studio-pane-h2h"></div></wa-tab-panel>
              <wa-tab-panel name="history"><div class="studio-pane studio-pane-history"></div></wa-tab-panel>
              <wa-tab-panel name="log"><div class="studio-pane studio-pane-log"></div></wa-tab-panel>
            </wa-tab-group>
          </div>
        </div>
      </div>
    </div>
  </div>`;

// Announce the active ribbon for the global float manager.
function announceRibbon(el) {
  window.dispatchEvent(new CustomEvent(APP_EVT.RIBBON_ACTIVE, { detail: { el } }));
}

// Persist a split's two flex-grow values; restore applies them back.
function persistSplit(key, a, b) {
  saveJson(key, [parseFloat(a.style.flexGrow), parseFloat(b.style.flexGrow)]);
}
function restoreSplit(key, a, b) {
  const g = loadJson(key);
  if (!Array.isArray(g) || g.length !== 2) return;
  a.style.flexGrow = String(g[0]);
  b.style.flexGrow = String(g[1]);
}

// Generic two-pane splitter: dragging the grip trades flex-grow between
// sibling panes a and b. Total grow maps linearly to total pixels, so the
// drag tracks the pointer 1:1. Mirrors the play-dock grip math.
function attachSplitter(grip, a, b, axis, persistKey) {
  grip.addEventListener("pointerdown", (eDown) => {
    if (eDown.button !== 0) return;
    eDown.preventDefault();
    try { grip.setPointerCapture(eDown.pointerId); } catch { /* */ }
    grip.classList.add("dragging");

    const vert = axis === AXIS.Y;
    const aRect = a.getBoundingClientRect();
    const bRect = b.getBoundingClientRect();
    const pxRange = vert ? aRect.height + bRect.height : aRect.width + bRect.width;
    const aG0 = parseFloat(getComputedStyle(a).flexGrow) || DEFAULT_SPLIT_GROW;
    const bG0 = parseFloat(getComputedStyle(b).flexGrow) || DEFAULT_SPLIT_GROW;
    const sumG = aG0 + bG0;
    const p0 = vert ? eDown.clientY : eDown.clientX;
    const a0 = vert ? aRect.height : aRect.width;

    const onMove = (e) => {
      const d = (vert ? e.clientY : e.clientX) - p0;
      const aPx = Math.max(0, Math.min(a0 + d, pxRange));
      a.style.flexGrow = String((aPx / pxRange) * sumG);
      b.style.flexGrow = String(((pxRange - aPx) / pxRange) * sumG);
    };
    const onUp = () => {
      grip.classList.remove("dragging");
      grip.removeEventListener("pointermove", onMove);
      grip.removeEventListener("pointerup", onUp);
      grip.removeEventListener("pointercancel", onUp);
      persistSplit(persistKey, a, b);
    };
    grip.addEventListener("pointermove", onMove);
    grip.addEventListener("pointerup", onUp);
    grip.addEventListener("pointercancel", onUp);
  });
}

// ---- Tourneys list -------------------------------------------------------

// Selection invariant: a non-empty list always has exactly one selected
// row. When the persisted selection is gone, fall back to the first row.
function ensureSelection(ctx) {
  if (ctx.tournaments.some((t) => t.id === ctx.selectedId)) return;
  setSelected(ctx, ctx.tournaments[0]?.id ?? null);
}

// Persisted so the last selection survives reloads; null clears the key.
function setSelected(ctx, id) {
  ctx.selectedId = id;
  saveRaw(STORAGE_KEY.STUDIO_SELECTED_ID, id);
}

// Click handler: repaint selection in place (preserves scroll), then rebind
// the live store, standings/log panes, and ribbon to the selection.
function studioSelect(ctx, id) {
  if (ctx.selectedId === id) return;
  setSelected(ctx, id);
  for (const tr of ctx.tourneysPaneEl.querySelectorAll(".studio-tourney-row")) {
    tr.classList.toggle("selected", tr.dataset.id === id);
  }
  syncLive(ctx);
  syncRibbon(ctx);
  paintWall(ctx);
}

function selectedTournament(ctx) {
  return ctx.tournaments.find((t) => t.id === ctx.selectedId) || null;
}

// Tourney table columns: one descriptor list drives the sort cycle (firstDir)
// and the stack sorter (field / tiebreak). Games is display-only; created reads
// created_at, which also breaks ties so order is stable.
const TOURNEY_SORT_COLS = [
  { key: "status", firstDir: SORT_DIR.ASC },
  { key: "created", firstDir: SORT_DIR.DESC, field: "created_at", tiebreak: true },
  { key: "name", firstDir: SORT_DIR.ASC },
  { key: "games", sortable: false },
];

// Sort the list by the MRU sort stack; an empty stack keeps the server order.
function sortedStudioTourneys(ctx) {
  const arr = ctx.tournaments.slice();
  sortByStack(arr, ctx.tourneyStack(), TOURNEY_SORT_COLS);
  return arr;
}

// Build the tourney table once: a sticky sortable header + a tbody the row
// renderer fills. Layered MRU sort + arrows/persistence come from attachLayeredSort.
function buildTourneyTable(ctx) {
  const pane = ctx.tourneysPaneEl;
  if (!pane) return;
  const wrap = document.createElement("div");
  wrap.className = "studio-tourney-wrap";
  const table = document.createElement("table");
  table.className = "wb-table studio-tourney-tbl";
  table.innerHTML = `<colgroup><col><col><col><col></colgroup>
    <thead><tr>
      <th>Status<span class="th-grip"></span></th>
      <th>Created<span class="th-grip"></span></th>
      <th>Name<span class="th-grip"></span></th>
      <th class="studio-tourney-games-col">Completed</th>
    </tr></thead><tbody></tbody>`;
  wrap.appendChild(table);
  pane.replaceChildren(wrap);
  ctx.tourneyTbody = table.querySelector("tbody");
  ctx.tourneyStack = attachLayeredSort({
    table, columns: TOURNEY_SORT_COLS,
    sortKey: STORAGE_KEY.STUDIO_TOURNEY_SORT, stackKey: STORAGE_KEY.STUDIO_TOURNEY_STACK,
    onChange: () => renderTourneys(ctx),
  }).get;
  const colEls = Array.from(table.querySelectorAll("col"));
  attachColumnResize({
    table,
    grips: Array.from(table.querySelectorAll(".th-grip")),
    overlayHost: wrap,
    storageKey: STORAGE_KEY.STUDIO_TOURNEY_COL_PCTS,
    sizes: STUDIO_TOURNEY_DEFAULT_PCTS.slice(),
    unit: "pct",
    applySizes: makePctApplySizes(colEls, STUDIO_TOURNEY_MIN_PCT),
  });
}

// Compact local date+time for the Created column; falls back to the raw
// string if it isn't a parseable timestamp.
function formatCreated(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// Games cell: while running, Arena's progress bar (label then bar); otherwise
// "played / total".
function gamesCell(t, played, total) {
  if (t.status === STATUS.RUNNING && total) {
    return `<div class="studio-progress">${progressLabelHtml(played, total)}${progressBarHtml(played, total)}</div>`;
  }
  return total ? `${played} / ${total}` : (played ? String(played) : "");
}

function studioTourneyRow(ctx, t) {
  const tr = document.createElement("tr");
  tr.className = "studio-tourney-row" + (t.id === ctx.selectedId ? " selected" : "");
  tr.dataset.id = t.id;
  const total = totalGames(t);
  const played = t.standings?.games ?? 0;
  tr.innerHTML =
    `<td class="studio-tourney-status">${statusBadgeHtml(t.status)}${sprtBadgeHtml(t)}</td>` +
    `<td class="studio-tourney-created">${escapeHtml(formatCreated(t.created_at))}</td>` +
    `<td class="studio-tourney-name" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</td>` +
    `<td class="studio-tourney-games">${gamesCell(t, played, total)}</td>`;
  tr.addEventListener("click", () => studioSelect(ctx, t.id));
  tr.addEventListener("dblclick", () => { studioSelect(ctx, t.id); ctx.actions?.info(t); });
  return tr;
}

function renderTourneys(ctx) {
  if (!ctx.tourneyTbody) return;
  ensureSelection(ctx);
  if (ctx.tournaments.length === 0) {
    // Empty state lives on the Boards wall (paintWall), not in this table.
    ctx.tourneyTbody.replaceChildren();
    syncRibbon(ctx);
    paintWall(ctx);
    return;
  }
  ctx.tourneyTbody.replaceChildren(...sortedStudioTourneys(ctx).map((t) => studioTourneyRow(ctx, t)));
  syncRibbon(ctx);
  paintWall(ctx);
}

// ---- Ribbon actions ------------------------------------------------------
// Reuse Arena's tournament verbs via the shared adapter; target the selected
// tourney. New is always enabled; the rest follow status like Arena.
function wireRibbonActions(ctx) {
  const q = (sel) => ctx.ribbonEl.querySelector(sel);
  ctx.ribbonBtns = {
    create: q(".studio-new"), start: q(".studio-start"), stop: q(".studio-stop"),
    info: q(".studio-info"), edit: q(".studio-edit"), remove: q(".studio-remove"),
  };
  ctx.actions = tournamentActions({
    api: ctx.api, log: ctx.log,
    getSettings: () => ctx.tournSettings,
    reload: () => studioLoadList(ctx),
  });
  const onSel = (fn) => () => { const t = selectedTournament(ctx); if (t) fn(t); };
  ctx.ribbonBtns.create.addEventListener("click", () => ctx.actions.create());
  ctx.ribbonBtns.start.addEventListener("click", onSel(ctx.actions.start));
  ctx.ribbonBtns.stop.addEventListener("click", onSel(ctx.actions.stop));
  ctx.ribbonBtns.info.addEventListener("click", onSel(ctx.actions.info));
  ctx.ribbonBtns.edit.addEventListener("click", onSel(ctx.actions.edit));
  ctx.ribbonBtns.remove.addEventListener("click", onSel(ctx.actions.remove));
}

function syncRibbon(ctx) {
  const b = ctx.ribbonBtns;
  if (!b) return;
  const t = selectedTournament(ctx);
  if (!t) {
    for (const k of ["start", "stop", "info", "edit", "remove"]) b[k].disabled = true;
    return;
  }
  const isActive = t.id === ctx.activeId;
  const anotherRunning = ctx.activeId !== null && !isActive;
  const status = t.status;
  const isRestart = status === STATUS.STOPPED || status === STATUS.FAILED;
  b.start.disabled = isActive || anotherRunning || status === STATUS.RUNNING || status === STATUS.DONE;
  b.stop.disabled = !isActive;
  b.remove.disabled = isActive;
  b.info.disabled = false;
  b.edit.disabled = isActive || status === STATUS.DONE;
  const icon = b.start.querySelector("wa-icon");
  if (icon) icon.setAttribute("name", isRestart ? "rotate-right" : "play");
  const startLabel = isRestart ? "Restart" : "Start";
  b.start.setAttribute("aria-label", startLabel);
  b.start.setAttribute("title", startLabel);
}

// Fetch the tournament list; a generation guard drops out-of-order
// responses so a slow reload can't clobber a newer one.
async function studioLoadList(ctx) {
  const gen = ++ctx.listGen;
  let body;
  try {
    body = await ctx.api("GET", TOURNAMENTS_ENDPOINT);
  } catch (e) {
    reportError({ log: ctx.log }, LOAD_FAIL_MSG, e);
    return;
  }
  if (gen !== ctx.listGen || !ctx.tourneysPaneEl) return;
  ctx.tournaments = body.tournaments || [];
  ctx.activeId = body.active_id ?? null;
  renderTourneys(ctx);
  syncLive(ctx);
}

// ---- Selection binding ---------------------------------------------------
// Engines/Games/Boards show the *running* tourney's live state (empty unless
// it's selected). Standings/Event Log bind to the selected tourney (any):
// the running one is kept fresh by the live store; others load a snapshot.

function syncLive(ctx) {
  const runningSelected = ctx.selectedId && ctx.selectedId === ctx.activeId;
  if (runningSelected) {
    if (ctx.liveTid !== ctx.selectedId) startLive(ctx, ctx.selectedId);
  } else {
    stopLive(ctx);
    loadSelected(ctx);
  }
}

// Snapshot standings + event log for a non-running selected tourney. Guarded
// so repeated list reloads don't re-fetch the same selection.
function loadSelected(ctx, force = false) {
  if (!force && ctx.selLoadedId === ctx.selectedId) return;
  ctx.selLoadedId = ctx.selectedId;
  const tid = ctx.selectedId;
  const gen = ++ctx.selGen;
  if (!tid) { ctx.selDetail = null; ctx.selEvents = []; renderStandingsPane(ctx); renderLogPane(ctx); return; }
  ctx.api("GET", `${TOURNAMENTS_ENDPOINT}/${tid}`)
    .then((d) => { if (gen === ctx.selGen) { ctx.selDetail = d; renderStandingsPane(ctx); paintWall(ctx); } })
    .catch(() => {});
  ctx.api("GET", `${TOURNAMENTS_ENDPOINT}/${tid}/events`)
    .then((r) => { if (gen === ctx.selGen) { ctx.selEvents = buildLog(r.events); renderLogPane(ctx); } })
    .catch(() => {});
}

// Replay raw /events through the log pipeline so entries match live shape.
function buildLog(events) {
  const s = createLiveState();
  for (const e of (events || [])) if (e.kind?.startsWith(EVT_PREFIX)) addLogEntry(s, e);
  return s.eventLog;
}

function startLive(ctx, tid) {
  stopLive(ctx);
  ctx.live = createLiveState();
  ctx.liveTid = tid;
  ctx.liveUnsub = ctx.events.on((evt) => livePushEvent(ctx, evt));
  const gen = ++ctx.liveGen;
  ctx.selGen++; // supersede any pending snapshot load
  // Resolves once this start's boards are restored and fully drawn; the
  // initial perspective reveal (ready) awaits it so no empty slots flash.
  ctx.boardsRestored = Promise.all([
    ctx.api("GET", `${TOURNAMENTS_ENDPOINT}/${tid}`),
    ctx.api("GET", `${TOURNAMENTS_ENDPOINT}/${tid}/events`),
  ]).then(async ([detail, ev]) => {
    if (gen !== ctx.liveGen || !ctx.live) return;
    seedFromDetail(ctx.live, detail);
    for (const e of (ev.events || [])) if (e.kind?.startsWith(EVT_PREFIX)) addLogEntry(ctx.live, e);
    ctx.selDetail = detail;
    renderLivePanes(ctx);
    renderStandingsPane(ctx);
    renderLogPane(ctx);
    paintWall(ctx);
    // Wait for the board style so restored boards aren't styled with the
    // default; re-check liveness after the await.
    await ctx.boardStyleReady;
    if (gen === ctx.liveGen && ctx.live) await restoreBoards(ctx);
  }).catch((e) => reportError({ log: ctx.log }, LOAD_FAIL_MSG, e));
}

function stopLive(ctx) {
  ctx.liveUnsub?.();
  ctx.liveUnsub = null;
  ctx.live = null;
  ctx.liveTid = null;
  ctx.liveGen++;
  // Force the next loadSelected to re-fetch (e.g. a finished tourney that was
  // running+selected needs its final REST snapshot).
  ctx.selLoadedId = null;
  closeAllLiveGames();
  renderLivePanes(ctx);
}

// Always-on (selection-independent) crash surfacing: a tournament can fail
// while the user is inspecting a different one, so this fires from the list
// subscriber, not the per-live-session handler.
function toastRunnerCrash(ctx, evt) {
  if (evt.payload?.kind !== KIND.RUNNER_CRASH) return;
  const tid = evt.payload?.tournament_id;
  const t = ctx.tournaments.find((x) => x.id === tid);
  const name = t ? t.name : "Tournament";
  toast(`${name} failed: ${crashErrorLine(evt.payload)}`, {
    variant: "danger", duration: CRASH_TOAST_DURATION_MS,
  });
}

// Maintain the live maps from the WS stream; coalesce pane repaints and keep
// standings fresh (re-fetch on game/status changes).
function livePushEvent(ctx, evt) {
  if (!evt?.kind?.startsWith(EVT_PREFIX) || !ctx.live) return;
  const tid = evt.payload?.tournament_id;
  if (tid && tid !== ctx.liveTid) return;
  const inner = evt.payload?.kind;
  addLogEntry(ctx.live, evt);
  applyEventKind(ctx.live, evt, inner, ctx.liveTid);
  // A reconcile resolves a watched game -> persist so reload can rehydrate
  // it as a frozen board.
  if (inner === KIND.GAME_RECONCILED) saveBoards(ctx);
  if (evt.kind === EVT.STATUS || inner === KIND.GAME_FINISHED || inner === KIND.DONE || inner === KIND.STOPPED) {
    ctx.refreshStandings();
  }
  if (ctx._panesPending) return;
  ctx._panesPending = true;
  requestAnimationFrame(() => { ctx._panesPending = false; renderLivePanes(ctx); renderLogPane(ctx); });
}

function renderStandingsPane(ctx) {
  if (ctx.standingsBodyEl) renderStandings(ctx.standingsBodyEl, ctx.selDetail, true);
  // H2H and History share standings' data (selDetail) + cadence; repaint
  // them from the same sites.
  if (ctx.h2hBodyEl) renderH2H(ctx.h2hBodyEl, ctx.selDetail);
  renderHistory(ctx);
}

// History game-record field keys: the single source for column keys, row
// properties, and the comparator `field`. white/black reuse SIDE; num is the
// synthetic 1-based game order.
const HK = Object.freeze({ NUM: "num", RESULT: "result", OPENING: "opening" });

// Result sorts by meaning, not text: White win -> draw -> Black win, with
// unfinished/unknown last (ascending); num breaks ties.
const RESULT_RANK = Object.freeze({
  [RESULT.WHITE_WIN]: 0,
  [RESULT.DRAW]: 1,
  [RESULT.BLACK_WIN]: 2,
});

// History table columns: num is the numeric game order and the stable
// tiebreak; white/black/opening sort as text, result by RESULT_RANK.
const HISTORY_SORT_COLS = [
  { key: HK.NUM, firstDir: SORT_DIR.ASC, numeric: true, tiebreak: true },
  { key: SIDE.WHITE, firstDir: SORT_DIR.ASC },
  { key: SIDE.BLACK, firstDir: SORT_DIR.ASC },
  { key: HK.RESULT, firstDir: SORT_DIR.ASC, rank: RESULT_RANK },
  { key: HK.OPENING, firstDir: SORT_DIR.ASC },
];

// Build the History table once: sticky sortable header + a tbody the row
// renderer fills (mirrors buildTourneyTable). Result is the last column so it
// carries no resize grip; it is still sortable.
function buildHistoryTable(ctx) {
  const pane = ctx.historyPaneEl;
  if (!pane) return;
  const wrap = document.createElement("div");
  wrap.className = "studio-history-wrap";
  const table = document.createElement("table");
  table.className = "wb-table studio-history-tbl";
  table.innerHTML = `<colgroup><col><col><col><col><col></colgroup>
    <thead><tr>
      <th>#<span class="th-grip"></span></th>
      <th>White<span class="th-grip"></span></th>
      <th>Black<span class="th-grip"></span></th>
      <th>Result<span class="th-grip"></span></th>
      <th>Opening</th>
    </tr></thead><tbody></tbody>`;
  wrap.appendChild(table);
  pane.replaceChildren(wrap);
  ctx.historyTbody = table.querySelector("tbody");
  ctx.historyStack = attachLayeredSort({
    table, columns: HISTORY_SORT_COLS,
    sortKey: STORAGE_KEY.STUDIO_HISTORY_SORT, stackKey: STORAGE_KEY.STUDIO_HISTORY_STACK,
    onChange: () => renderHistory(ctx),
  }).get;
  const colEls = Array.from(table.querySelectorAll("col"));
  attachColumnResize({
    table,
    grips: Array.from(table.querySelectorAll(".th-grip")),
    overlayHost: wrap,
    storageKey: STORAGE_KEY.STUDIO_HISTORY_COL_PCTS,
    sizes: STUDIO_HISTORY_DEFAULT_PCTS.slice(),
    unit: "pct",
    applySizes: makePctApplySizes(colEls, STUDIO_HISTORY_MIN_PCT),
  });
}

function historyRow(ctx, tid, r) {
  const tr = document.createElement("tr");
  tr.className = "studio-history-row";
  tr.tabIndex = 0;
  tr.setAttribute("role", "button");
  tr.title = `Review game ${r.num}`;
  tr.innerHTML =
    `<td class="studio-history-num">${r.num}</td>` +
    `<td title="${escapeHtml(r.white)}">${escapeHtml(r.white)}</td>` +
    `<td title="${escapeHtml(r.black)}">${escapeHtml(r.black)}</td>` +
    `<td class="studio-history-result">${escapeHtml(r.result)}</td>` +
    `<td title="${escapeHtml(r.opening)}">${escapeHtml(r.opening)}</td>`;
  const open = () => replayTournamentGame({ tournamentId: tid, gameN: r.num, token: ctx.token })
    .catch((e) => reportError({ log: ctx.log }, REVIEW_FAIL_MSG, e));
  tr.addEventListener("click", open);
  tr.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
  });
  return tr;
}

// Completed games for the selected tourney. The 1-based row number is the
// game number the replay endpoint expects (same DECISIVE_RESULTS filter, no
// dedup), so a click resolves to the same PGN slice regardless of sort.
function renderHistory(ctx) {
  const tb = ctx.historyTbody;
  if (!tb) return;
  const tid = ctx.selDetail?.id;
  const games = ctx.selDetail?.games;
  if (!tid || !games || games.length === 0) {
    tb.replaceChildren();
    return;
  }
  const rows = games.map((g, i) => ({
    [HK.NUM]: i + 1, [SIDE.WHITE]: g.white, [SIDE.BLACK]: g.black,
    [HK.RESULT]: g.result, [HK.OPENING]: g.opening || "",
  }));
  sortByStack(rows, ctx.historyStack(), HISTORY_SORT_COLS);
  tb.replaceChildren(...rows.map((r) => historyRow(ctx, tid, r)));
}

function renderLogPane(ctx) {
  if (!ctx.logListEl) return;
  const running = ctx.selectedId && ctx.selectedId === ctx.activeId;
  const events = (running && ctx.live) ? ctx.live.eventLog : (ctx.selEvents || []);
  renderEventLogList(ctx.logListEl, events, ctx.logPaneEl);
}

function renderLivePanes(ctx) {
  renderEnginesPane(ctx);
  renderGamesPane(ctx);
}

function liveRow(iconHtml, label) {
  const li = document.createElement("li");
  li.className = "wb-sched-live";
  li.innerHTML = `<span class="wb-sched-icon" aria-hidden="true">${iconHtml}</span>` +
    `<span class="wb-sched-game" title="${escapeHtml(label)}">${escapeHtml(label)}</span>`;
  return li;
}

function watchBtn(ctx, attachKey, openOpts) {
  const btn = document.createElement("button");
  btn.className = "wb-sched-attach-btn";
  btn.textContent = "watch";
  btn.title = attachKey;
  btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(attachKey));
  btn.addEventListener("click", () => studioWatch(ctx, btn, attachKey, openOpts));
  return btn;
}

function renderEnginesPane(ctx) {
  const pane = ctx.enginesPaneEl;
  if (!pane) return;
  const proxies = ctx.live?.activeProxies;
  if (!proxies || proxies.size === 0) {
    pane.innerHTML = `<div class="wb-empty">${NO_ENGINES_MSG}</div>`;
    return;
  }
  const ul = document.createElement("ul");
  ul.className = "wb-sched-list";
  for (const [pid, p] of proxies) {
    const label = p.engineName || pid;
    const li = liveRow("&#9881;", label);
    li.appendChild(watchBtn(ctx, pid, { proxyId: pid, label, engineName: label }));
    ul.appendChild(li);
  }
  pane.replaceChildren(ul);
}

function renderGamesPane(ctx) {
  const pane = ctx.gamesPaneEl;
  if (!pane) return;
  const pairings = ctx.live?.livePairings;
  if (!pairings || pairings.size === 0) {
    pane.innerHTML = `<div class="wb-empty">${NO_GAMES_MSG}</div>`;
    return;
  }
  // Both proxies map to the same info object; dedupe per pair.
  const ul = document.createElement("ul");
  ul.className = "wb-sched-list";
  const shown = new Set();
  for (const [, info] of pairings) {
    const key = [info.proxyA, info.proxyB].sort().join(":");
    if (shown.has(key)) continue;
    shown.add(key);
    const wLabel = info.sideA === SIDE.WHITE ? info.engineA : info.engineB;
    const bLabel = info.sideA === SIDE.WHITE ? info.engineB : info.engineA;
    const li = liveRow("&#9822;", `${wLabel} - ${bLabel}`);
    const attachKey = info.pairId || key;
    li.appendChild(watchBtn(ctx, attachKey, {
      proxyId: info.proxyA, gameId: info.pairId || null,
      label: `${wLabel} vs ${bLabel}`, engineName: wLabel,
    }));
    ul.appendChild(li);
  }
  pane.replaceChildren(ul);
}

// ---- Boards (live game watch) --------------------------------------------
// Boards are WinBoxes rooted in the scrollable region, laid out by index in
// a 4-column grid (see boardCell for sizing). No free drag; re-gridded on
// open/close/resize.

// 4 columns on desktop, 1 on mobile (fill model -- the region stays bounded).
function studioCols() {
  return mqMobile.matches ? 1 : STUDIO_COLS;
}

// Cell size: width fills the region across the columns (>= the Arena board
// minimum), height keeps the board square (width + the fixed window chrome).
function boardCell(ctx) {
  const cols = studioCols();
  const w = ctx.boardsEl?.clientWidth ?? 0;
  const cw = Math.max(LIVE_MIN_WIDTH,
    Math.floor((w - 2 * STUDIO_BOARD_PAD - STUDIO_BOARD_GAP * (cols - 1)) / cols));
  return { cw, ch: cw + (LIVE_MIN_HEIGHT() - LIVE_MIN_WIDTH), cols };
}

// Reposition every open board into its slot; size the scroll canvas to the
// grid extent. A maximized board is refit to the (expanded) region;
// minimized windows keep their dock geometry.
function regridBoards(ctx) {
  if (!ctx.boardsEl) return;
  const { cw, ch, cols } = boardCell(ctx);
  // Only laid-out boards take slots; minimized (trayed) and maximized boards
  // are skipped so visible boards pack with no gaps.
  let slot = 0;
  for (const wb of getLiveWindows()) {
    if (wb.min) continue;
    if (wb.max) { fillRegion(ctx, wb); continue; }
    const col = slot % cols, row = Math.floor(slot / cols);
    wb.resize(cw, ch).move(
      STUDIO_BOARD_PAD + col * (cw + STUDIO_BOARD_GAP),
      STUDIO_BOARD_PAD + row * (ch + STUDIO_BOARD_GAP),
    );
    slot++;
  }
  sizeCanvas(ctx, slot, cw, ch, cols);
  // Single source of truth for board presence (laid-out or maximized).
  const hasBoards = anyBoardsShown();
  // The info wall fills the region when no boards are shown; boards hide it
  // (paintWall also toggles the .no-boards scroll-clip class).
  paintWall(ctx, hasBoards);
  // Mobile: pin the board area to one board (plus the top/bottom inset so it
  // fits exactly); collapse to nothing when there are no boards rather than
  // reserve a blank strip. Desktop lets the flex split govern the height.
  ctx.boardsEl.style.height = mqMobile.matches ? (hasBoards ? `${ch + 2 * STUDIO_BOARD_PAD}px` : "0") : "";
}

// Any board occupying the region (laid-out or maximized; minimized don't count).
function anyBoardsShown() {
  return getLiveWindows().some((wb) => !wb.min);
}

// Repaint the info wall from the selected tourney (prefer REST detail for the
// fuller template/started_at, fall back to the list row). The wall is a
// constant low-opacity backdrop on desktop -- behind boards when present.
// Mobile collapses the region when no boards, so hide it there to avoid a
// stray strip.
function paintWall(ctx, hasBoards = anyBoardsShown()) {
  if (!ctx.boardsWallEl) return;
  ctx.boardsWallEl.hidden = hasBoards && mqMobile.matches;
  // No boards -> region is pure (clipped) art: no scrollbar. Toggled here too
  // (not only in regridBoards) so the idle first paint, which never re-grids,
  // still clips.
  ctx.boardsEl?.classList.toggle("no-boards", !hasBoards);
  // No tourneys at all: the wall hosts the New-tournament CTA, not poster art.
  ctx.boardsWallEl.classList.toggle("is-empty", ctx.tournaments.length === 0);
  if (ctx.tournaments.length === 0) {
    renderEmptyWall(ctx);
    return;
  }
  const t = ctx.selDetail && ctx.selDetail.id === ctx.selectedId
    ? ctx.selDetail : selectedTournament(ctx);
  renderInfoWall(ctx.boardsWallEl, t);
}

// Empty-state CTA on the Boards wall: Arena's shared "+" prose, fired through
// the ribbon's create verb.
function renderEmptyWall(ctx) {
  const msg = document.createElement("p");
  msg.className = "studio-empty";
  msg.append(...newTournamentCta(() => ctx.actions?.create()));
  ctx.boardsWallEl.replaceChildren(msg);
}

// Canvas defines the scrollable extent (both axes) of the board grid. Zero
// when there are no laid-out boards so the region reserves nothing (no stray
// scrollbar / blank strip).
function sizeCanvas(ctx, count, cw, ch, gridCols) {
  if (!ctx.boardsCanvasEl) return;
  if (count <= 0) {
    ctx.boardsCanvasEl.style.width = "0";
    ctx.boardsCanvasEl.style.height = "0";
    return;
  }
  const cols = Math.min(gridCols, Math.max(1, count));
  const rows = Math.max(1, Math.ceil(count / gridCols));
  ctx.boardsCanvasEl.style.width = `${2 * STUDIO_BOARD_PAD + cols * cw + (cols - 1) * STUDIO_BOARD_GAP}px`;
  ctx.boardsCanvasEl.style.height = `${2 * STUDIO_BOARD_PAD + rows * ch + (rows - 1) * STUDIO_BOARD_GAP}px`;
}

// Size a board to fill the visible region, pinned to its top-left.
function fillRegion(ctx, wb) {
  wb.resize(ctx.boardsEl.clientWidth, ctx.boardsEl.clientHeight).move(0, 0);
}

// Scroll the region so a (grid-placed) board is in view. Already fully visible
// -> no scroll. Otherwise always anchor the board's TOP to the view top: a
// consistent anchor avoids the top/bottom flip-flop (which made repeated Watch
// clicks jump the region up and down, especially for boards taller than the
// viewport).
function scrollBoardIntoView(ctx, wb) {
  const region = ctx.boardsEl;
  if (!region || wb.min) return;
  const viewTop = region.scrollTop;
  const viewBottom = viewTop + region.clientHeight;
  const fullyVisible = wb.y >= viewTop && wb.y + wb.height <= viewBottom;
  if (fullyVisible) return;
  region.scrollTo({ top: wb.y - STUDIO_BOARD_PAD, behavior: "smooth" });
}

// Maximize (option b): grow the Boards split to the full main column, lock
// region scroll, and fill it with this board. The bottom tab row collapses.
function maximizeBoard(ctx, wb) {
  ctx._maxWb = wb;
  if (!ctx._savedSplit) {
    ctx._savedSplit = { boards: ctx.boardsEl.style.flexGrow, bottom: ctx.bottomEl.style.flexGrow };
  }
  ctx.boardsEl.style.flexGrow = "1";
  ctx.bottomEl.style.flexGrow = "0";
  ctx.boardsEl.style.overflow = "hidden";
  ctx.boardsEl.scrollTop = 0;
  // Split grows on the next layout; fill once the region has its new size.
  requestAnimationFrame(() => { if (ctx.boardsEl && wb.max) fillRegion(ctx, wb); });
}

// revertSplit=false leaves boards 1 / bottom 0 in place so a grip-initiated
// restore keeps the grip under the cursor; the in-flight drag owns the split.
function restoreBoard(ctx, revertSplit = true) {
  ctx._maxWb = null;
  if (ctx._savedSplit) {
    if (revertSplit) {
      ctx.boardsEl.style.flexGrow = ctx._savedSplit.boards;
      ctx.bottomEl.style.flexGrow = ctx._savedSplit.bottom;
    }
    ctx._savedSplit = null;
  }
  ctx.boardsEl.style.overflow = "";
  requestAnimationFrame(() => regridBoards(ctx));
}

// Rebuild the minimize tray from the currently minimized boards. Native
// minimize is hidden via CSS; each minimized board gets a restore chip.
function renderTray(ctx) {
  const tray = ctx.boardsTrayEl;
  if (!tray) return;
  const mins = getLiveWindows().filter((wb) => wb.min);
  tray.replaceChildren();
  for (const wb of mins) {
    // Mini winbox title bar: title restores, the native X closes. Mirror the
    // board's own header color so the chip reads as that minimized window.
    const chip = document.createElement("div");
    chip.className = "studio-tray-chip";
    const header = wb.g?.querySelector(".wb-header");
    if (header) chip.style.setProperty("--chip-bg", getComputedStyle(header).backgroundColor);
    chip.innerHTML = `<span class="studio-tray-chip-title"></span><span class="wb-close"></span>`;
    const [title, closeEl] = chip.children;
    title.textContent = title.title = wb._watchOpts?.label || "board";
    title.onclick = () => wb.restore();
    closeEl.onclick = () => wb.close();
    tray.appendChild(chip);
  }
  tray.hidden = mins.length === 0;
}

function studioSlotRect(ctx, i) {
  const { cw, ch, cols } = boardCell(ctx);
  const col = i % cols, row = Math.floor(i / cols);
  return {
    x: STUDIO_BOARD_PAD + col * (cw + STUDIO_BOARD_GAP),
    y: STUDIO_BOARD_PAD + row * (ch + STUDIO_BOARD_GAP),
    w: cw, h: ch,
  };
}

// Sync every watch button's live state to the open boards. Called when a
// board closes (X) so the spawning button stops showing as live.
function refreshWatchButtons(ctx) {
  for (const pane of [ctx.enginesPaneEl, ctx.gamesPaneEl]) {
    if (!pane) continue;
    for (const btn of pane.querySelectorAll(".wb-sched-attach-btn")) {
      btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(btn.title));
    }
  }
}

// Min/restore both reflow the grid (reverting the split if this was the
// maximized board), repaint the tray, and persist the open-board set.
function wireBoardHooks(ctx, wb) {
  // No tray change on maximize, so persist without the renderTray repaint.
  wb.onmaximize = () => { maximizeBoard(ctx, wb); saveBoards(ctx); };
  const repaint = () => { renderTray(ctx); saveBoards(ctx); };
  // Minimizing a maximized board stashes the intent (WinBox clears wb.max) and
  // reverts the split; the chip then restores it straight back to maximized.
  wb.onminimize = () => {
    if (wb === ctx._maxWb) { wb._wasMax = true; restoreBoard(ctx); } else regridBoards(ctx);
    repaint();
  };
  wb.onrestore = () => {
    if (wb._wasMax) { wb._wasMax = false; remaximize(wb); }
    else if (wb === ctx._maxWb) restoreBoard(ctx, !ctx._restoreViaGrip);
    else regridBoards(ctx);
    repaint();
  };
}

// Maximize a board out of band: from onrestore, a synchronous maximize() is
// undone by WinBox restore()'s own re-check of this.max; on reload, later board
// opens would steal focus. The microtask (pre-paint, so no grid flash) sidesteps
// both. focus() raises it to the foreground.
function remaximize(wb) {
  queueMicrotask(() => { if (wb.g) wb.maximize().focus(); });
}

// Next laid-out slot index (minimized/maximized boards don't occupy slots).
function nextSlotRect(ctx) {
  return studioSlotRect(ctx, getLiveWindows().filter((wb) => !wb.min && !wb.max).length);
}

// Shared post-open: wire hooks, re-grid, repaint tray, persist.
function placeBoard(ctx, res) {
  if (res?.wb && !res.alreadyOpen) wireBoardHooks(ctx, res.wb);
  regridBoards(ctx);
  renderTray(ctx);
  saveBoards(ctx);
  return res;
}

// Open a live board into the next slot. initialRect avoids a flash at
// WinBox's default size. flash=true is the amber attention flash, wanted only
// for a genuine Watch click -- restore passes false (silent reopen).
function openBoard(ctx, openOpts, min, flash = true) {
  return placeBoard(ctx, openLiveGameWindow({
    ...openOpts, token: ctx.token, tournamentId: ctx.liveTid,
    root: ctx.boardsEl, variantClass: STUDIO_BOARD_CLASS,
    boardStyle: ctx.boardStyleCached, top: 0, left: 0, right: 0, min, flash,
    initialRect: nextSlotRect(ctx),
  }));
}

// Reopen a finished game as a frozen board (rehydrated from PGN), like Arena.
// Re-seed resolvedGames so a later snapshot keeps `resolved` (otherwise the
// freshly-rebuilt live store loses it and the next reload drops the board).
function openFrozenBoard(ctx, b, flash = true) {
  if (b.gameId && ctx.live) ctx.live.resolvedGames.set(b.gameId, b.resolved);
  return placeBoard(ctx, openFrozenGameWindow({
    proxyId: b.proxyId, gameId: b.gameId, label: b.label, engineName: b.engineName,
    token: ctx.token, tournamentId: ctx.liveTid,
    gameN: b.resolved.gameN, result: b.resolved.result, termination: b.resolved.termination,
    root: ctx.boardsEl, variantClass: STUDIO_BOARD_CLASS,
    boardStyle: ctx.boardStyleCached, top: 0, left: 0, right: 0, min: !!b.min, flash,
    initialRect: nextSlotRect(ctx),
  }));
}

function studioWatch(ctx, btn, attachKey, openOpts) {
  const res = openBoard(ctx, openOpts, false);
  if (res?.wb) scrollBoardIntoView(ctx, res.wb);
  btn?.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(attachKey));
}

// ---- Board persistence ---------------------------------------------------
// Persist the open boards per running tourney so a reload restores them
// (like Arena). Geometry isn't saved -- boards are grid-placed by order.

// A board's snapshot carries `resolved` (gameN/result/termination) once its
// game has reconciled, so a reload can rehydrate it as a frozen board.
function snapshotBoards(ctx) {
  return getLiveWindows().filter((wb) => wb._watchOpts).map((wb) => {
    const gameId = wb._watchOpts.gameId ?? null;
    const resolved = gameId ? ctx.live?.resolvedGames.get(gameId) : null;
    return {
      proxyId: wb._watchOpts.proxyId, gameId,
      label: wb._watchOpts.label, engineName: wb._watchOpts.engineName,
      min: !!wb.min, max: !!(wb.max || wb._wasMax),
      ...(resolved ? { resolved } : {}),
    };
  });
}

// No-op while liveTid is null (teardown) so closing boards on stop/switch
// doesn't wipe the saved set.
function saveBoards(ctx) {
  if (!ctx.liveTid) return;
  saveJson(STORAGE_KEY.STUDIO_BOARDS_PREFIX + ctx.liveTid, snapshotBoards(ctx));
}

// Reopen saved boards: resolved ones as frozen (from PGN), still-live ones
// live; drop those that ended while away with no resolution. Resolves once
// every reopened board is fully drawn, so the caller can gate a reveal.
function restoreBoards(ctx) {
  const saved = loadJson(STORAGE_KEY.STUDIO_BOARDS_PREFIX + ctx.liveTid);
  if (!Array.isArray(saved)) return Promise.resolve();
  const opened = [];
  for (const b of saved) {
    let res;
    if (b.resolved) {
      res = openFrozenBoard(ctx, b, false);
    } else {
      const live = b.gameId
        ? ctx.live.livePairings.get(b.proxyId)?.pairId === b.gameId
        : ctx.live.activeProxies.has(b.proxyId);
      if (live) res = openBoard(ctx, { proxyId: b.proxyId, gameId: b.gameId, label: b.label, engineName: b.engineName }, !!b.min, false);
    }
    if (res?.wb && b.max) {
      // Trayed-while-maximized: chip restores to max. Visible: maximize now.
      if (b.min) res.wb._wasMax = true; else remaximize(res.wb);
    }
    if (res?.wb?._ready) opened.push(res.wb._ready);
  }
  refreshWatchButtons(ctx);
  return Promise.all(opened);
}

// Restore each bottom group's active tab and persist changes.
function wireTabPersistence(ctx) {
  const groups = [
    [ctx.container.querySelector(".studio-bottom-left .studio-tabs"), STORAGE_KEY.STUDIO_TAB_LEFT, STUDIO_TAB_DEFAULT_LEFT],
    [ctx.container.querySelector(".studio-bottom-right .studio-tabs"), STORAGE_KEY.STUDIO_TAB_RIGHT, STUDIO_TAB_DEFAULT_RIGHT],
  ];
  for (const [el, key, def] of groups) {
    if (!el) continue;
    el.setAttribute("active", loadRaw(key) || def);
    el.addEventListener("wa-tab-show", (ev) => {
      if (ev.detail?.name) saveRaw(key, ev.detail.name);
    });
  }
}

// Ctrl/Cmd+A inside a bottom tab-group selects just the active tab's
// contents (the visible panel); Ctrl+C then copies it with each row on one
// line (flex rows otherwise split across lines).
function wireTabClipboard(ctx) {
  for (const group of ctx.container.querySelectorAll(".studio-tabs")) {
    selectContentsOnCtrlA(group, () => group.querySelector("wa-tab-panel[active]"));
    copyRowsAsLines(group);
  }
}

function wireSplitters(ctx) {
  restoreSplit(STORAGE_KEY.STUDIO_SPLIT_ROW, ctx.boardsEl, ctx.bottomEl);
  restoreSplit(STORAGE_KEY.STUDIO_SPLIT_COL, ctx.bottomLeftEl, ctx.bottomRightEl);
  // Grabbing the grip while a board is maximized un-maximizes it, but keeps
  // boards 1 / bottom 0 so the grip stays under the cursor; the drag then
  // owns the new split. Capture phase: run before the splitter's own handler.
  ctx.gripRowEl.addEventListener("pointerdown", () => {
    const maxed = getLiveWindows().find((wb) => wb.max);
    if (!maxed) return;
    ctx._restoreViaGrip = true;
    maxed.restore();
    ctx._restoreViaGrip = false;
  }, true);
  attachSplitter(ctx.gripRowEl, ctx.boardsEl, ctx.bottomEl, AXIS.Y, STORAGE_KEY.STUDIO_SPLIT_ROW);
  attachSplitter(ctx.gripColEl, ctx.bottomLeftEl, ctx.bottomRightEl, AXIS.X, STORAGE_KEY.STUDIO_SPLIT_COL);
}

// Thin shell: render the static frame, collect refs into ctx, wire the
// resizers, announce the ribbon. Region behavior lands in later steps.
export function mountTournamentStudio({ container, api, events, log, token }) {
  container.innerHTML = STUDIO_HTML;
  const q = (sel) => container.querySelector(sel);
  const ctx = {
    api, events, log, token, container,
    panel: q(".studio-panel"),
    ribbonEl: q(".studio-ribbon"),
    boardsEl: q(".studio-boards"),
    boardsCanvasEl: q(".studio-boards-canvas"),
    boardsWallEl: q(".studio-boards-wall"),
    boardsTrayEl: q(".studio-boards-tray"),
    bottomEl: q(".studio-bottom"),
    bottomLeftEl: q(".studio-bottom-left"),
    bottomRightEl: q(".studio-bottom-right"),
    gripRowEl: q(".studio-grip-row"),
    gripColEl: q(".studio-grip-col"),
    tourneysPaneEl: q(".studio-pane-tourneys"),
    enginesPaneEl: q(".studio-pane-engines"),
    gamesPaneEl: q(".studio-pane-livegames"),
    standingsPaneEl: q(".studio-pane-standings"),
    h2hPaneEl: q(".studio-pane-h2h"),
    historyPaneEl: q(".studio-pane-history"),
    logPaneEl: q(".studio-pane-log"),
    // Tourneys data + selection (selection restored from last session).
    tournaments: [], activeId: null, listGen: 0,
    selectedId: loadRaw(STORAGE_KEY.STUDIO_SELECTED_ID),
    // Sort stacks are getter fns assigned by build{Tourney,History}Table.
    tourneyTbody: null, tourneyStack: null,
    historyTbody: null, historyStack: null,
    // Live runner store for the running tourney (null unless it's selected).
    live: null, liveTid: null, liveUnsub: null, liveGen: 0, _panesPending: false,
    // Resolves when a running tourney's boards finish restoring (gates reveal).
    boardsRestored: null,
    // Standings/Event Log bound to the selected tourney (any).
    selDetail: null, selEvents: [], selGen: 0, selLoadedId: null,
    // Board style for live boards (fetched once, like the workspace).
    boardStyleCached: null, boardStyleReady: null,
    // Ribbon verbs (shared with Arena) + tournament settings for New/Edit.
    tournSettings: null, ribbonBtns: null, actions: null,
  };
  // Standings pane reuses the shared resizable-column table; log pane is a
  // plain list the shared renderer fills.
  ctx.standingsBodyEl = makeStandingsBody();
  ctx.standingsPaneEl?.appendChild(ctx.standingsBodyEl);
  // H2H body (build once, resizable columns wired); fill shares standings' cadence.
  ctx.h2hBodyEl = makeH2HBody();
  ctx.h2hPaneEl?.appendChild(ctx.h2hBodyEl);
  ctx.logListEl = document.createElement("ul");
  ctx.logListEl.className = "wb-eventlog-list";
  ctx.logPaneEl?.appendChild(ctx.logListEl);
  ctx.refreshStandings = debounce(() => {
    if (!ctx.liveTid) return;
    ctx.api("GET", `${TOURNAMENTS_ENDPOINT}/${ctx.liveTid}`)
      .then((d) => { if (ctx.liveTid) { ctx.selDetail = d; renderStandingsPane(ctx); paintWall(ctx); } })
      .catch(() => {});
  }, STANDINGS_REFRESH_DEBOUNCE_MS);
  buildTourneyTable(ctx);
  buildHistoryTable(ctx);
  wireSplitters(ctx);
  wireTabPersistence(ctx);
  wireTabClipboard(ctx);
  wireRibbonActions(ctx);
  announceRibbon(ctx.ribbonEl);

  ctx.boardStyleReady = ctx.api("GET", SETTINGS_ENDPOINT)
    .then((s) => { ctx.boardStyleCached = s?.board_style || null; })
    .catch(() => {});
  ctx.api("GET", TOURNAMENT_SETTINGS_ENDPOINT)
    .then((s) => { ctx.tournSettings = s; })
    .catch(() => {});

  // Re-grid boards when one closes or the viewport changes; closing also
  // resets the spawning watch button. If the closed board was the maximized
  // one, revert the split (onclose doesn't fire onrestore).
  ctx.onBoardClosed = () => {
    if (ctx._savedSplit && !getLiveWindows().some((wb) => wb.max)) restoreBoard(ctx);
    else regridBoards(ctx);
    renderTray(ctx);
    refreshWatchButtons(ctx);
    saveBoards(ctx);
  };
  ctx.onBoardResize = debounce(() => regridBoards(ctx), BOARD_RESIZE_DEBOUNCE_MS);
  // Re-pin/re-grid immediately when the mobile breakpoint flips (cols and
  // board-area height change between desktop and mobile).
  ctx.onMqMobile = () => regridBoards(ctx);
  window.addEventListener(APP_EVT.LIVEGAME_CLOSED, ctx.onBoardClosed);
  window.addEventListener("resize", ctx.onBoardResize);
  mqMobile.addEventListener("change", ctx.onMqMobile);

  // Reload the list on any tournament event (coalesced); initial load now.
  ctx.reload = debounce(() => studioLoadList(ctx), LIST_RELOAD_DEBOUNCE_MS);
  ctx.offEvents = ctx.events.on((evt) => { toastRunnerCrash(ctx, evt); ctx.reload(); });

  // `ready` gates the router's reveal until built: first list load (table +
  // wall), then any restored boards drawn (boardsRestored), then a flushed
  // frame. Raced against a timeout so a stuck fetch can't hide the UI forever.
  const built = studioLoadList(ctx)
    .then(() => ctx.boardsRestored)
    .catch(() => {})
    .then(() => new Promise((r) => requestAnimationFrame(() => r())));
  const ready = Promise.race([
    built,
    new Promise((r) => setTimeout(r, READY_TIMEOUT_MS)),
  ]);

  return { ready, unmount: () => unmountStudio(ctx) };
}

function unmountStudio(ctx) {
  ctx.offEvents?.();
  // Remove the board-closed listener before stopLive's closeAllLiveGames so
  // teardown doesn't re-save (and wipe) the board set.
  window.removeEventListener(APP_EVT.LIVEGAME_CLOSED, ctx.onBoardClosed);
  window.removeEventListener("resize", ctx.onBoardResize);
  mqMobile.removeEventListener("change", ctx.onMqMobile);
  // stopLive unsubscribes, nulls liveTid (so a pending standings refresh
  // no-ops), and closes the boards.
  stopLive(ctx);
  announceRibbon(null);
  ctx.panel.remove();
  ctx.panel = ctx.ribbonEl = null;
  ctx.boardsEl = ctx.boardsCanvasEl = ctx.boardsWallEl = ctx.boardsTrayEl = ctx.bottomEl = ctx.bottomLeftEl = ctx.bottomRightEl = null;
  ctx.gripRowEl = ctx.gripColEl = ctx.tourneysPaneEl = ctx.tourneyTbody = null;
  ctx.enginesPaneEl = ctx.gamesPaneEl = null;
  ctx.standingsPaneEl = ctx.standingsBodyEl = ctx.h2hPaneEl = ctx.h2hBodyEl = ctx.historyPaneEl = ctx.historyTbody = ctx.logPaneEl = ctx.logListEl = null;
  ctx.ribbonBtns = ctx.actions = null;
}
