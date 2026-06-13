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
import { renderTournamentRow } from "./tournament-row.js";
import { reportError } from "./dialogs.js";
import { debounce, escapeHtml } from "./wb-utils.js";
import { EVT_PREFIX } from "./tournament-events.js";
import { SIDE } from "./chess-consts.js";
import { addLogEntry, applyEventKind, createLiveState, seedFromDetail } from "./tournament-live-state.js";

const TOURNAMENTS_ENDPOINT = "/api/tournaments";
const LOAD_FAIL_MSG = "Loading tournaments failed";
const NO_ENGINES_MSG = "No active engines.";
const NO_GAMES_MSG = "No games in play.";
// Coalesce bursty WS events into one list reload.
const LIST_RELOAD_DEBOUNCE_MS = 150;

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
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button class="ribbon-btn ribbon-btn--danger studio-remove" disabled aria-label="Remove" title="Remove">
          <wa-icon name="trash"></wa-icon>
        </button>
      </div>

      <div class="studio-main">
        <div class="studio-header"></div>
        <div class="studio-boards"></div>
        <div class="studio-grip-row" role="separator" aria-orientation="horizontal"></div>
        <div class="studio-bottom">
          <div class="studio-bottom-left">
            <wa-tab-group class="studio-tabs">
              <wa-tab panel="engines">Engines</wa-tab>
              <wa-tab panel="livegames">Games</wa-tab>
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

// Click handler: repaint selection in place (preserves list scroll).
// Header / Standings / Event Log rebind to the selection in later steps.
function studioSelect(ctx, id) {
  if (ctx.selectedId === id) return;
  setSelected(ctx, id);
  for (const li of ctx.tourneysPaneEl.querySelectorAll(".tournament-row")) {
    li.classList.toggle("selected", li.dataset.id === id);
  }
  syncLive(ctx);
}

function renderTourneys(ctx) {
  if (!ctx.tourneysPaneEl) return;
  ensureSelection(ctx);
  const ul = document.createElement("ul");
  ul.className = "tournaments-list";
  ul.setAttribute("role", "listbox");
  for (const t of ctx.tournaments) {
    ul.appendChild(renderTournamentRow(t, {
      selected: t.id === ctx.selectedId,
      onSelect: (t) => studioSelect(ctx, t.id),
    }));
  }
  ctx.tourneysPaneEl.replaceChildren(ul);
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

// ---- Live runner (Engines / Games panes) ---------------------------------
// Engines and Games show the *running* tournament's live state. They have
// content only when the selected tourney is the active one; otherwise the
// live store is torn down and the panes go empty.

function syncLive(ctx) {
  const runningSelected = ctx.selectedId && ctx.selectedId === ctx.activeId;
  if (runningSelected) {
    if (ctx.liveTid !== ctx.selectedId) startLive(ctx, ctx.selectedId);
  } else {
    stopLive(ctx);
  }
}

function startLive(ctx, tid) {
  stopLive(ctx);
  ctx.live = createLiveState();
  ctx.liveTid = tid;
  ctx.liveUnsub = ctx.events.on((evt) => livePushEvent(ctx, evt));
  const gen = ++ctx.liveGen;
  ctx.api("GET", `${TOURNAMENTS_ENDPOINT}/${tid}`)
    .then((detail) => {
      if (gen !== ctx.liveGen || !ctx.live) return;
      seedFromDetail(ctx.live, detail);
      renderLivePanes(ctx);
    })
    .catch((e) => reportError({ log: ctx.log }, LOAD_FAIL_MSG, e));
}

function stopLive(ctx) {
  ctx.liveUnsub?.();
  ctx.liveUnsub = null;
  ctx.live = null;
  ctx.liveTid = null;
  ctx.liveGen++;
  renderLivePanes(ctx);
}

// Maintain the live maps from the WS stream; coalesce pane repaints.
function livePushEvent(ctx, evt) {
  if (!evt?.kind?.startsWith(EVT_PREFIX) || !ctx.live) return;
  const tid = evt.payload?.tournament_id;
  if (tid && tid !== ctx.liveTid) return;
  addLogEntry(ctx.live, evt);
  applyEventKind(ctx.live, evt, evt.payload?.kind, ctx.liveTid);
  if (ctx._panesPending) return;
  ctx._panesPending = true;
  requestAnimationFrame(() => { ctx._panesPending = false; renderLivePanes(ctx); });
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
  for (const [pid, p] of proxies) ul.appendChild(liveRow("&#9881;", p.engineName || pid));
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
    ul.appendChild(liveRow("&#9822;", `${wLabel} - ${bLabel}`));
  }
  pane.replaceChildren(ul);
}

function wireSplitters(ctx) {
  restoreSplit(STORAGE_KEY.STUDIO_SPLIT_ROW, ctx.boardsEl, ctx.bottomEl);
  restoreSplit(STORAGE_KEY.STUDIO_SPLIT_COL, ctx.bottomLeftEl, ctx.bottomRightEl);
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
    headerEl: q(".studio-header"),
    boardsEl: q(".studio-boards"),
    bottomEl: q(".studio-bottom"),
    bottomLeftEl: q(".studio-bottom-left"),
    bottomRightEl: q(".studio-bottom-right"),
    gripRowEl: q(".studio-grip-row"),
    gripColEl: q(".studio-grip-col"),
    tourneysPaneEl: q(".studio-pane-tourneys"),
    enginesPaneEl: q(".studio-pane-engines"),
    gamesPaneEl: q(".studio-pane-livegames"),
    // Tourneys data + selection (selection restored from last session).
    tournaments: [], activeId: null, listGen: 0,
    selectedId: loadRaw(STORAGE_KEY.STUDIO_SELECTED_ID),
    // Live runner store for the running tourney (null unless it's selected).
    live: null, liveTid: null, liveUnsub: null, liveGen: 0, _panesPending: false,
  };
  wireSplitters(ctx);
  announceRibbon(ctx.ribbonEl);

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
  announceRibbon(null);
  ctx.panel.remove();
  ctx.panel = ctx.ribbonEl = ctx.headerEl = null;
  ctx.boardsEl = ctx.bottomEl = ctx.bottomLeftEl = ctx.bottomRightEl = null;
  ctx.gripRowEl = ctx.gripColEl = ctx.tourneysPaneEl = null;
  ctx.enginesPaneEl = ctx.gamesPaneEl = null;
}
