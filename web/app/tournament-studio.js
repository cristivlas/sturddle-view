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
import { closeAllLiveGames, getLiveWindows, isLiveWindowOpen, LIVE_MIN_HEIGHT, LIVE_MIN_WIDTH, openLiveGameWindow } from "./tournament-live-game.js";
import { mqMobile } from "./breakpoints.js";

const TOURNAMENTS_ENDPOINT = "/api/tournaments";
const SETTINGS_ENDPOINT = "/settings";
const LOAD_FAIL_MSG = "Loading tournaments failed";
const NO_ENGINES_MSG = "No active engines.";
const NO_GAMES_MSG = "No games in play.";
// Coalesce bursty WS events into one list reload.
const LIST_RELOAD_DEBOUNCE_MS = 150;

// Boards region grid: always 4 columns; cells stretch to fill the region
// width (keeping the board square), clamped to the Arena minimum board
// width -- below that the row overflows and scrolls. Unlimited rows scroll
// vertically. Boards are laid out (no-move), absolutely positioned.
const STUDIO_COLS = 4;
const STUDIO_BOARD_GAP = 6;
const STUDIO_BOARD_CLASS = "sturddle-wb-studio no-move";
const BOARD_RESIZE_DEBOUNCE_MS = 120;

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
        <div class="studio-boards"><div class="studio-boards-canvas"></div></div>
        <div class="studio-boards-tray" hidden></div>
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
  closeAllLiveGames();
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
    Math.floor((w - STUDIO_BOARD_GAP * (cols - 1)) / cols));
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
    wb.resize(cw, ch).move(col * (cw + STUDIO_BOARD_GAP), row * (ch + STUDIO_BOARD_GAP));
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
  ctx.boardsCanvasEl.style.width = `${cols * cw + (cols - 1) * STUDIO_BOARD_GAP}px`;
  ctx.boardsCanvasEl.style.height = `${rows * ch + (rows - 1) * STUDIO_BOARD_GAP}px`;
}

// Size a board to fill the visible region, pinned to its top-left.
function fillRegion(ctx, wb) {
  wb.resize(ctx.boardsEl.clientWidth, ctx.boardsEl.clientHeight).move(0, 0);
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
    x: col * (cw + STUDIO_BOARD_GAP), y: row * (ch + STUDIO_BOARD_GAP),
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

function studioWatch(ctx, btn, attachKey, openOpts) {
  // Open directly at the next slot so the board doesn't flash at WinBox's
  // default geometry before the re-grid. The slot index is the count of
  // laid-out boards -- minimized/maximized ones don't occupy slots.
  const laidOut = getLiveWindows().filter((wb) => !wb.min && !wb.max).length;
  const res = openLiveGameWindow({
    ...openOpts, token: ctx.token, tournamentId: ctx.liveTid,
    root: ctx.boardsEl, variantClass: STUDIO_BOARD_CLASS,
    boardStyle: ctx.boardStyleCached, top: 0, left: 0, right: 0,
    initialRect: studioSlotRect(ctx, laidOut),
  });
  if (res?.wb && !res.alreadyOpen) {
    res.wb.onmaximize = () => maximizeBoard(ctx, res.wb);
    // Minimizing frees a slot -- reflow survivors and add a tray chip. If
    // the maximized board is the one minimized, revert the expanded split.
    res.wb.onminimize = () => {
      if (res.wb === ctx._maxWb) restoreBoard(ctx); else regridBoards(ctx);
      renderTray(ctx);
    };
    // Restore fires for both un-maximize and un-minimize; only revert the
    // split when this board is the maximized one.
    res.wb.onrestore = () => {
      if (res.wb === ctx._maxWb) restoreBoard(ctx); else regridBoards(ctx);
      renderTray(ctx);
    };
  }
  regridBoards(ctx);
  btn?.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(attachKey));
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
    headerEl: q(".studio-header"),
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
    // Tourneys data + selection (selection restored from last session).
    tournaments: [], activeId: null, listGen: 0,
    selectedId: loadRaw(STORAGE_KEY.STUDIO_SELECTED_ID),
    // Live runner store for the running tourney (null unless it's selected).
    live: null, liveTid: null, liveUnsub: null, liveGen: 0, _panesPending: false,
    // Board style for live boards (fetched once, like the workspace).
    boardStyleCached: null,
  };
  wireSplitters(ctx);
  announceRibbon(ctx.ribbonEl);

  ctx.api("GET", SETTINGS_ENDPOINT)
    .then((s) => { ctx.boardStyleCached = s?.board_style || null; })
    .catch(() => {});

  // Re-grid boards when one closes or the viewport changes; closing also
  // resets the spawning watch button. If the closed board was the maximized
  // one, revert the split (onclose doesn't fire onrestore).
  ctx.onBoardClosed = () => {
    if (ctx._savedSplit && !getLiveWindows().some((wb) => wb.max)) restoreBoard(ctx);
    else regridBoards(ctx);
    renderTray(ctx);
    refreshWatchButtons(ctx);
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
  ctx.panel = ctx.ribbonEl = ctx.headerEl = null;
  ctx.boardsEl = ctx.boardsCanvasEl = ctx.boardsTrayEl = ctx.bottomEl = ctx.bottomLeftEl = ctx.bottomRightEl = null;
  ctx.gripRowEl = ctx.gripColEl = ctx.tourneysPaneEl = null;
  ctx.enginesPaneEl = ctx.gamesPaneEl = null;
}
