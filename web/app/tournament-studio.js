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
import { totalGames } from "./tournament-row.js";
import { attachColumnSort } from "./col-sort.js";
import { attachColumnResize } from "./col-resize.js";
import { reportError } from "./dialogs.js";
import { debounce, escapeHtml } from "./wb-utils.js";
import { EVT, EVT_PREFIX, KIND, STATUS } from "./tournament-events.js";
import { tournamentActions } from "./tournaments.js";
import { SIDE } from "./chess-consts.js";
import { addLogEntry, applyEventKind, createLiveState, seedFromDetail } from "./tournament-live-state.js";
import { closeAllLiveGames, getLiveWindows, isLiveWindowOpen, LIVE_MIN_HEIGHT, LIVE_MIN_WIDTH, openLiveGameWindow } from "./tournament-live-game.js";
import { makeStandingsBody, renderStandings } from "./tournament-standings.js";
import { renderEventLogList } from "./tournament-eventlog.js";
import { mqMobile } from "./breakpoints.js";

const TOURNAMENTS_ENDPOINT = "/api/tournaments";
const TOURNAMENT_SETTINGS_ENDPOINT = "/api/tournament-settings";
const SETTINGS_ENDPOINT = "/settings";
const LOAD_FAIL_MSG = "Loading tournaments failed";
const NO_ENGINES_MSG = "No active engines.";
const NO_GAMES_MSG = "No games in play.";
// Coalesce bursty WS events into one list reload.
const LIST_RELOAD_DEBOUNCE_MS = 150;
// Coalesce standings re-fetches while the selected tourney is running.
const STANDINGS_REFRESH_DEBOUNCE_MS = 400;

// Boards region grid: always 4 columns; cells stretch to fill the region
// width (keeping the board square), clamped to the Arena minimum board
// width -- below that the row overflows and scrolls. Unlimited rows scroll
// vertically. Boards are laid out (no-move), absolutely positioned.
const STUDIO_COLS = 4;
const STUDIO_BOARD_GAP = 0;
// Inset so boards don't sit flush against the region border (abs-positioned
// boards ignore container padding, so the offset is applied in placement).
const STUDIO_BOARD_PAD = 1;
const STUDIO_BOARD_CLASS = "sturddle-wb-studio no-move";
const BOARD_RESIZE_DEBOUNCE_MS = 120;
// Default active tab per bottom group (first tab) when none is remembered.
const STUDIO_TAB_DEFAULT_LEFT = "livegames";
const STUDIO_TAB_DEFAULT_RIGHT = "tourneys";
// Tourney table default column widths (Status, Name, Games) + resize floor.
const STUDIO_TOURNEY_DEFAULT_PCTS = [20, 52, 28];
const STUDIO_TOURNEY_MIN_PCT = 10;

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

// Read the persisted UX mode; anything unrecognized falls back to Arena.
export function getTournamentUx() {
  const v = loadRaw(STORAGE_KEY.TOURNAMENT_UX);
  return v === TOURNAMENT_UX.STUDIO ? TOURNAMENT_UX.STUDIO : TOURNAMENT_UX.ARENA;
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
        <div class="studio-boards"><div class="studio-boards-canvas"></div></div>
        <div class="studio-boards-tray" hidden></div>
        <div class="studio-grip-row" role="separator" aria-orientation="horizontal"></div>
        <div class="studio-bottom">
          <div class="studio-bottom-left">
            <wa-tab-group class="studio-tabs">
              <wa-tab panel="livegames">Games</wa-tab>
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
              <wa-tab panel="log">Event Log</wa-tab>
              <wa-tab-panel name="tourneys"><div class="studio-pane studio-pane-tourneys"></div></wa-tab-panel>
              <wa-tab-panel name="standings"><div class="studio-pane studio-pane-standings"></div></wa-tab-panel>
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
}

function selectedTournament(ctx) {
  return ctx.tournaments.find((t) => t.id === ctx.selectedId) || null;
}

// Sort the list by the active column (Status or Name); created_at breaks ties
// so order is stable. No active sort keeps the server order.
function sortedStudioTourneys(ctx) {
  const arr = ctx.tournaments.slice();
  const s = ctx.tourneySort;
  if (!s) return arr;
  const dir = s.dir === "asc" ? 1 : -1;
  const tie = (a, b) => (a.created_at || "").localeCompare(b.created_at || "");
  arr.sort((a, b) => {
    if (s.key === "name") {
      return dir * (a.name || "").localeCompare(b.name || "", undefined, { sensitivity: "base" }) || tie(a, b);
    }
    const av = a.status || "", bv = b.status || "";
    return av !== bv ? dir * av.localeCompare(bv) : tie(a, b);
  });
  return arr;
}

// Build the tourney table once: a sticky sortable header + a tbody the row
// renderer fills. Sort cycling/arrows/persistence come from attachColumnSort.
function buildTourneyTable(ctx) {
  const pane = ctx.tourneysPaneEl;
  if (!pane) return;
  const wrap = document.createElement("div");
  wrap.className = "studio-tourney-wrap";
  const table = document.createElement("table");
  table.className = "wb-table studio-tourney-tbl";
  table.innerHTML = `<colgroup><col><col><col></colgroup>
    <thead><tr>
      <th data-col="status">Status<span class="th-grip"></span></th>
      <th data-col="name">Name<span class="th-grip"></span></th>
      <th class="studio-tourney-games-col">Games</th>
    </tr></thead><tbody></tbody>`;
  wrap.appendChild(table);
  pane.replaceChildren(wrap);
  ctx.tourneyTbody = table.querySelector("tbody");
  const sortCtrl = attachColumnSort({
    table,
    columns: [
      { key: "status", firstDir: "asc" },
      { key: "name", firstDir: "asc" },
      { key: "games", sortable: false },
    ],
    storageKey: STORAGE_KEY.STUDIO_TOURNEY_SORT,
    onSort: (state) => { ctx.tourneySort = state; renderTourneys(ctx); },
  });
  ctx.tourneySort = sortCtrl.current();
  const colEls = Array.from(table.querySelectorAll("col"));
  attachColumnResize({
    table,
    grips: Array.from(table.querySelectorAll(".th-grip")),
    overlayHost: wrap,
    storageKey: STORAGE_KEY.STUDIO_TOURNEY_COL_PCTS,
    sizes: STUDIO_TOURNEY_DEFAULT_PCTS.slice(),
    unit: "pct",
    applySizes(sizes, rctx) {
      if (rctx) {
        const { deltaFrac, startSizes, gripIdx } = rctx;
        const dPct = deltaFrac * 100;
        let a = startSizes[gripIdx] + dPct;
        let b = startSizes[gripIdx + 1] - dPct;
        if (a < STUDIO_TOURNEY_MIN_PCT) { b -= STUDIO_TOURNEY_MIN_PCT - a; a = STUDIO_TOURNEY_MIN_PCT; }
        if (b < STUDIO_TOURNEY_MIN_PCT) { a -= STUDIO_TOURNEY_MIN_PCT - b; b = STUDIO_TOURNEY_MIN_PCT; }
        sizes[gripIdx] = a;
        sizes[gripIdx + 1] = b;
      }
      colEls.forEach((c, i) => { c.style.width = sizes[i] + "%"; });
    },
  });
}

// Games cell: while running, the same progress bar Arena's tourney list
// shows; otherwise "played / total".
function gamesCell(t, played, total) {
  if (t.status === STATUS.RUNNING && total) {
    const pct = Math.min(100, Math.round((played / total) * 100));
    return `<div class="studio-progress">` +
      `<div class="tournament-progress" role="progressbar" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${played}">` +
      `<div class="tournament-progress-fill" style="width: ${pct}%"></div></div>` +
      `<span class="tournament-progress-label">${played} / ${total} &middot; ${pct}%</span>` +
      `</div>`;
  }
  return total ? `${played} / ${total}` : (played ? String(played) : "");
}

function studioTourneyRow(ctx, t) {
  const tr = document.createElement("tr");
  tr.className = "studio-tourney-row" + (t.id === ctx.selectedId ? " selected" : "");
  tr.dataset.id = t.id;
  const total = totalGames(t);
  const played = t.standings?.games ?? 0;
  const sprt = t.template?.sprt ? ` <span class="tournament-sprt-badge">SPRT</span>` : "";
  tr.innerHTML =
    `<td><span class="tournament-status status-${t.status}">${escapeHtml(t.status)}</span></td>` +
    `<td class="studio-tourney-name" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}${sprt}</td>` +
    `<td class="studio-tourney-games">${gamesCell(t, played, total)}</td>`;
  tr.addEventListener("click", () => studioSelect(ctx, t.id));
  tr.addEventListener("dblclick", () => { studioSelect(ctx, t.id); ctx.actions?.info(t); });
  return tr;
}

function renderTourneys(ctx) {
  if (!ctx.tourneyTbody) return;
  ensureSelection(ctx);
  ctx.tourneyTbody.replaceChildren(...sortedStudioTourneys(ctx).map((t) => studioTourneyRow(ctx, t)));
  syncRibbon(ctx);
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
    .then((d) => { if (gen === ctx.selGen) { ctx.selDetail = d; renderStandingsPane(ctx); } })
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
  Promise.all([
    ctx.api("GET", `${TOURNAMENTS_ENDPOINT}/${tid}`),
    ctx.api("GET", `${TOURNAMENTS_ENDPOINT}/${tid}/events`),
  ]).then(([detail, ev]) => {
    if (gen !== ctx.liveGen || !ctx.live) return;
    seedFromDetail(ctx.live, detail);
    for (const e of (ev.events || [])) if (e.kind?.startsWith(EVT_PREFIX)) addLogEntry(ctx.live, e);
    ctx.selDetail = detail;
    renderLivePanes(ctx);
    renderStandingsPane(ctx);
    renderLogPane(ctx);
    restoreBoards(ctx);
  }).catch((e) => reportError({ log: ctx.log }, LOAD_FAIL_MSG, e));
}

function stopLive(ctx) {
  ctx.liveUnsub?.();
  ctx.liveUnsub = null;
  ctx.live = null;
  ctx.liveTid = null;
  ctx.liveGen++;
  closeAllLiveGames();
  renderLivePanes(ctx);
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
  if (evt.kind === EVT.STATUS || inner === KIND.GAME_FINISHED || inner === KIND.DONE || inner === KIND.STOPPED) {
    ctx.refreshStandings();
  }
  if (ctx._panesPending) return;
  ctx._panesPending = true;
  requestAnimationFrame(() => { ctx._panesPending = false; renderLivePanes(ctx); renderLogPane(ctx); });
}

function renderStandingsPane(ctx) {
  if (ctx.standingsBodyEl) renderStandings(ctx.standingsBodyEl, ctx.selDetail);
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
  li.innerHTML = `<span class="wb-sched-icon">${iconHtml}</span>` +
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
  // Mobile: pin the board area to one board; the page scrolls for the rest
  // and the board area scrolls internally for additional boards. Desktop
  // lets the flex split govern the height.
  ctx.boardsEl.style.height = mqMobile.matches ? `${ch}px` : "";
}

// Canvas defines the scrollable extent (both axes) of the board grid.
function sizeCanvas(ctx, count, cw, ch, gridCols) {
  if (!ctx.boardsCanvasEl) return;
  const cols = Math.min(gridCols, Math.max(1, count));
  const rows = Math.max(1, Math.ceil(count / gridCols));
  ctx.boardsCanvasEl.style.width = `${2 * STUDIO_BOARD_PAD + cols * cw + (cols - 1) * STUDIO_BOARD_GAP}px`;
  ctx.boardsCanvasEl.style.height = `${2 * STUDIO_BOARD_PAD + rows * ch + (rows - 1) * STUDIO_BOARD_GAP}px`;
}

// Size a board to fill the visible region, pinned to its top-left.
function fillRegion(ctx, wb) {
  wb.resize(ctx.boardsEl.clientWidth, ctx.boardsEl.clientHeight).move(0, 0);
}

// Scroll the region so a (grid-placed) board is fully in view.
function scrollBoardIntoView(ctx, wb) {
  const region = ctx.boardsEl;
  if (!region || wb.min) return;
  const viewTop = region.scrollTop;
  const viewBottom = viewTop + region.clientHeight;
  if (wb.y < viewTop) {
    region.scrollTo({ top: wb.y - STUDIO_BOARD_PAD, behavior: "smooth" });
  } else if (wb.y + wb.height > viewBottom) {
    region.scrollTo({ top: wb.y + wb.height - region.clientHeight + STUDIO_BOARD_PAD, behavior: "smooth" });
  }
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

function restoreBoard(ctx) {
  ctx._maxWb = null;
  if (ctx._savedSplit) {
    ctx.boardsEl.style.flexGrow = ctx._savedSplit.boards;
    ctx.bottomEl.style.flexGrow = ctx._savedSplit.bottom;
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
    const chip = document.createElement("button");
    chip.className = "studio-tray-chip";
    chip.textContent = wb._watchOpts?.label || "board";
    chip.title = chip.textContent;
    chip.addEventListener("click", () => wb.restore());
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
  wb.onmaximize = () => maximizeBoard(ctx, wb);
  const onChange = () => {
    if (wb === ctx._maxWb) restoreBoard(ctx); else regridBoards(ctx);
    renderTray(ctx);
    saveBoards(ctx);
  };
  wb.onminimize = onChange;
  wb.onrestore = onChange;
}

// Open one board into the next laid-out slot (minimized/maximized boards
// don't occupy slots). initialRect avoids a flash at WinBox's default size.
function openBoard(ctx, openOpts, min) {
  const laidOut = getLiveWindows().filter((wb) => !wb.min && !wb.max).length;
  const res = openLiveGameWindow({
    ...openOpts, token: ctx.token, tournamentId: ctx.liveTid,
    root: ctx.boardsEl, variantClass: STUDIO_BOARD_CLASS,
    boardStyle: ctx.boardStyleCached, top: 0, left: 0, right: 0, min,
    initialRect: studioSlotRect(ctx, laidOut),
  });
  if (res?.wb && !res.alreadyOpen) wireBoardHooks(ctx, res.wb);
  regridBoards(ctx);
  renderTray(ctx);
  saveBoards(ctx);
  return res;
}

function studioWatch(ctx, btn, attachKey, openOpts) {
  const res = openBoard(ctx, openOpts, false);
  if (res?.wb) scrollBoardIntoView(ctx, res.wb);
  btn?.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(attachKey));
}

// ---- Board persistence ---------------------------------------------------
// Persist the open boards per running tourney so a reload restores them
// (like Arena). Geometry isn't saved -- boards are grid-placed by order.

function snapshotBoards() {
  return getLiveWindows().filter((wb) => wb._watchOpts).map((wb) => ({
    proxyId: wb._watchOpts.proxyId, gameId: wb._watchOpts.gameId ?? null,
    label: wb._watchOpts.label, engineName: wb._watchOpts.engineName,
    min: !!wb.min,
  }));
}

// No-op while liveTid is null (teardown) so closing boards on stop/switch
// doesn't wipe the saved set.
function saveBoards(ctx) {
  if (!ctx.liveTid) return;
  saveJson(STORAGE_KEY.STUDIO_BOARDS_PREFIX + ctx.liveTid, snapshotBoards());
}

// Reopen saved boards that are still live in the seeded maps; drop the rest.
function restoreBoards(ctx) {
  const saved = loadJson(STORAGE_KEY.STUDIO_BOARDS_PREFIX + ctx.liveTid);
  if (!Array.isArray(saved)) return;
  for (const b of saved) {
    const live = b.gameId
      ? ctx.live.livePairings.get(b.proxyId)?.pairId === b.gameId
      : ctx.live.activeProxies.has(b.proxyId);
    if (!live) continue;
    openBoard(ctx, { proxyId: b.proxyId, gameId: b.gameId, label: b.label, engineName: b.engineName }, !!b.min);
  }
  refreshWatchButtons(ctx);
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

function wireSplitters(ctx) {
  restoreSplit(STORAGE_KEY.STUDIO_SPLIT_ROW, ctx.boardsEl, ctx.bottomEl);
  restoreSplit(STORAGE_KEY.STUDIO_SPLIT_COL, ctx.bottomLeftEl, ctx.bottomRightEl);
  // Resizing the Boards split while a board is maximized un-maximizes it
  // (reverts the split synchronously) so the drag starts from real
  // geometry. Capture phase: run before the splitter's own handler.
  ctx.gripRowEl.addEventListener("pointerdown", () => {
    getLiveWindows().find((wb) => wb.max)?.restore();
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
    logPaneEl: q(".studio-pane-log"),
    // Tourneys data + selection (selection restored from last session).
    tournaments: [], activeId: null, listGen: 0,
    selectedId: loadRaw(STORAGE_KEY.STUDIO_SELECTED_ID),
    tourneyTbody: null, tourneySort: null,
    // Live runner store for the running tourney (null unless it's selected).
    live: null, liveTid: null, liveUnsub: null, liveGen: 0, _panesPending: false,
    // Standings/Event Log bound to the selected tourney (any).
    selDetail: null, selEvents: [], selGen: 0, selLoadedId: null,
    // Board style for live boards (fetched once, like the workspace).
    boardStyleCached: null,
    // Ribbon verbs (shared with Arena) + tournament settings for New/Edit.
    tournSettings: null, ribbonBtns: null, actions: null,
  };
  // Standings pane reuses the shared resizable-column table; log pane is a
  // plain list the shared renderer fills.
  ctx.standingsBodyEl = makeStandingsBody();
  ctx.standingsPaneEl?.appendChild(ctx.standingsBodyEl);
  ctx.logListEl = document.createElement("ul");
  ctx.logListEl.className = "wb-eventlog-list";
  ctx.logPaneEl?.appendChild(ctx.logListEl);
  ctx.refreshStandings = debounce(() => {
    if (!ctx.liveTid) return;
    ctx.api("GET", `${TOURNAMENTS_ENDPOINT}/${ctx.liveTid}`)
      .then((d) => { if (ctx.liveTid) { ctx.selDetail = d; renderStandingsPane(ctx); } })
      .catch(() => {});
  }, STANDINGS_REFRESH_DEBOUNCE_MS);
  buildTourneyTable(ctx);
  wireSplitters(ctx);
  wireTabPersistence(ctx);
  wireRibbonActions(ctx);
  announceRibbon(ctx.ribbonEl);

  ctx.api("GET", SETTINGS_ENDPOINT)
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
  ctx.offEvents = ctx.events.on(ctx.reload);
  studioLoadList(ctx);

  return { unmount: () => unmountStudio(ctx) };
}

function unmountStudio(ctx) {
  ctx.offEvents?.();
  ctx.liveUnsub?.();
  ctx.live = null;
  window.removeEventListener(APP_EVT.LIVEGAME_CLOSED, ctx.onBoardClosed);
  window.removeEventListener("resize", ctx.onBoardResize);
  mqMobile.removeEventListener("change", ctx.onMqMobile);
  closeAllLiveGames();
  announceRibbon(null);
  ctx.panel.remove();
  ctx.panel = ctx.ribbonEl = null;
  ctx.boardsEl = ctx.boardsCanvasEl = ctx.boardsTrayEl = ctx.bottomEl = ctx.bottomLeftEl = ctx.bottomRightEl = null;
  ctx.gripRowEl = ctx.gripColEl = ctx.tourneysPaneEl = ctx.tourneyTbody = null;
  ctx.enginesPaneEl = ctx.gamesPaneEl = null;
  ctx.standingsPaneEl = ctx.standingsBodyEl = ctx.logPaneEl = ctx.logListEl = null;
  ctx.ribbonBtns = ctx.actions = null;
}
