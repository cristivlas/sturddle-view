// Tournament workspace: WinBox windows for Standings, Schedule, Event log,
// plus on-demand Live Game windows (one per engine POV).
//
// State: one workspace per tab; opening a different tournament closes the
// prior one. Desktop state (open windows, position, size, min/max, z-order)
// is snapshotted at the explicit save points (close / closeAll / finalize)
// and restored on re-open. WinBox events are not monitored continuously.
//
// Data flow:
//   GET /api/tournaments/{id} on open -> seed standings + schedule.
//   WS `tournament_status`             -> refresh metadata + standings.
//   WS `tournament_update`             -> event log; refresh on game-finished.
//   Periodic GET while running         -> reconcile standings.

import {
  closeAllLiveGames, getLiveWindows,
  isLiveWindowOpen, openLiveGameWindow, openFrozenGameWindow,
  LIVE_MIN_WIDTH, LIVE_MIN_HEIGHT, DEBUG_WATCH,
} from "./tournament-live-game.js";
import { EVT, EVT_PREFIX, KIND, STATUS } from "./tournament-events.js";
import { CONFIRM_WIPE_QS, buildRestartConfirm } from "./tournament-restart.js";
import { attachColumnResize } from "./col-resize.js";
import { apiErrorDetail, confirm, toast } from "./dialogs.js";
import {
  AUTOSCROLL_SLACK_ROW_PX,
  escapeHtml,
  flashWindow,
  isPinnedToBottom,
  scrollToBottom,
} from "./wb-utils.js";
import { createSlotGrid, SLOT_GAP } from "./workspace-slot-grid.js";

const STORAGE_KEY_PREFIX = "sturddle:workspace:";
const STANDINGS_COL_PCTS_KEY = "sturddle:tournaments:standingsColPcts";
const STANDINGS_DEFAULT_PCTS = [22, 5, 5, 5, 5, 8, 25, 25];
const EVENT_LOG_LIMIT = 500;



function loadState(id) {
  try {
    const raw = localStorage.getItem(STORAGE_KEY_PREFIX + id);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveState(id, state) {
  try {
    localStorage.setItem(STORAGE_KEY_PREFIX + id, JSON.stringify(state));
  } catch {
    // Storage may be disabled (private mode quotas); best-effort.
  }
}

function hasOpenWindows(state) {
  return state !== null &&
    Object.entries(state).some(([k, v]) => k !== "_closed" && v?.open);
}

// Restorable: snapshot has open windows AND was not explicitly dismissed.
// Navigation uses this to decide whether to reopen.
export function hasSavedWorkspaceState(id) {
  const s = loadState(id);
  return s !== null && !s._closed && hasOpenWindows(s);
}

// Distinguishes "brand-new tournament" from "explicitly dismissed".
export function hasAnyDesktopState(id) {
  return loadState(id) !== null;
}

export function clearWorkspaceState(id) {
  try { localStorage.removeItem(STORAGE_KEY_PREFIX + id); } catch {}
}


const LAYOUT = Object.freeze({ NONE: 0, TIDY: 1, TILE: 2, SNAP: 3 });
const LAYOUT_STORAGE_KEY = "sturddle:active-layout";

let activeWorkspace = null;
let activeLayout = Number(localStorage.getItem(LAYOUT_STORAGE_KEY) ?? LAYOUT.NONE);
function setLayout(mode) {
  activeLayout = mode;
  localStorage.setItem(LAYOUT_STORAGE_KEY, mode);
}


export function openTournamentWorkspace({ api, events, log, token, tournament, top = 0, left = 0, getRight = () => window.innerWidth }) {
  // Single-active model. Re-clicking the workspace icon for the
  // already-open tournament is a no-op (just focus its windows) so
  // attached engine windows survive -- closing here would tear them
  // down via tearDown's closeAllLiveGames().
  if (activeWorkspace) {
    if (activeWorkspace.tournamentId === tournament.id) return activeWorkspace;
    activeWorkspace.close();
    activeWorkspace = null;
  }

  const savedState = loadState(tournament.id);
  // Restore-from-snapshot when there's any open window in the snapshot,
  // regardless of _closed (the ribbon always restores; _closed only blocks
  // navigation). lastGeometry holds last-known position/size per key so
  // closed slots can carry geometry forward into the next snapshot.
  const restoreFromSaved = hasOpenWindows(savedState);
  const MIN_SIZES = {
    standings: { minwidth: 320, minheight: 120 },
    schedule:  { minwidth: 320, minheight: 120 },
    engines:   { minwidth: 280, minheight: 120 },
    log:       { minwidth: 280, minheight: 120 },
  };
  const lastGeometry = {};
  for (const key of Object.keys(MIN_SIZES)) {
    const s = savedState?.[key];
    const ms = MIN_SIZES[key];
    lastGeometry[key] = s
      ? { x: s.x, y: s.y, width: s.width, height: s.height }
      : null;
  }
  let detail = null;
  const eventLog = [];
  // Server-stamped sequence numbers we've already added to eventLog.
  // Lets us run the WS subscription in parallel with the REST backfill
  // without showing duplicates around workspace open.
  const seenSeqs = new Set();
  let unsubscribe = null;
  // True when close() / closeAll() drove the tear-down. Distinguishes from
  // "user closed the last window manually" -- in that case finalize() is the
  // one that writes the snapshot (with _closed=true).
  let explicitlyClosed = false;
  // Idempotency guard: tearDown can be reached via close() and again via the
  // last onclose callback; finalize() must run exactly once.
  let finalized = false;
  // proxy_id -> { engineName }
  const activeProxies = new Map();
  // proxy_id -> { pairId, proxyA, engineA, sideA, proxyB, engineB, sideB }
  // Both proxies in a pair map to the same info object.
  const livePairings = new Map();
  // pair_id -> { gameN, result, termination }. Populated from
  // game_reconciled so snapshotLive() can mark resolved windows for
  // frozen-rehydration on a later workspace re-open.
  const resolvedGames = new Map();

  // Board style is fetched once per workspace open and reused for every
  // watch click. Avoids a /settings round-trip on each click and keeps
  // all live windows in this session visually consistent even if the
  // user changes the global setting mid-tournament.
  let boardStyleCached = null;
  api("GET", "/settings")
    .then(s => { boardStyleCached = s?.board_style || null; })
    .catch(() => {});

  // Slot grid hands out aligned rects for live-board windows. A slot is
  // free if no live window currently overlaps it, so dragging a window
  // out of its slot frees that slot without explicit bookkeeping. When
  // no slot fits, the new window is minimized -- WS still connects so
  // the live state stays current behind the minimize bar.
  const MAX_GRID_COLS = 4;
  const slotGrid = createSlotGrid({
    top, left, getRight,
    getCellWidth: () => {
      const cols = Math.min(MAX_GRID_COLS, Number(detail?.template?.games_in_parallel) || MAX_GRID_COLS);
      return Math.max(LIVE_MIN_WIDTH, Math.floor((getRight() - left - SLOT_GAP * (cols - 1)) / cols));
    },
    cellHeight: LIVE_MIN_HEIGHT,
    getWindows: () => getLiveWindows(),
    getMaxRows: () => activeLayout === LAYOUT.TIDY ? 1 : Infinity,
  });
  // ---- Window construction ----------------------------------------------

  function makeStandingsBody() {
    const el = document.createElement("div");
    el.className = "wb-standings";
    el.innerHTML = `
      <div class="wb-sprt-slot"></div>
      <div class="wb-partial-slot"></div>
      <div class="wb-empty wb-standings-empty">Loading...</div>
      <div class="wb-standings-table-wrap" hidden>
        <table class="wb-table wb-standings-tbl">
          <colgroup>
            <col><col><col><col><col><col><col><col>
          </colgroup>
          <thead>
            <tr>
              <th>Engine<span class="th-grip"></span></th>
              <th>G<span class="th-grip"></span></th>
              <th>W<span class="th-grip"></span></th>
              <th>L<span class="th-grip"></span></th>
              <th>D<span class="th-grip"></span></th>
              <th>Pts<span class="th-grip"></span></th>
              <th>%<span class="th-grip"></span></th>
              <th>Elo<span class="th-grip"></span></th>
              <th>Ordo</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    `;
    const wrapEl = el.querySelector(".wb-standings-table-wrap");
    const tableEl = el.querySelector(".wb-standings-tbl");
    const colEls = Array.from(el.querySelectorAll(".wb-standings-tbl col"));
    const grips = Array.from(el.querySelectorAll(".wb-standings-tbl .th-grip"));
    const colPcts = STANDINGS_DEFAULT_PCTS.slice();
    const minPct = 4;
    attachColumnResize({
      table: tableEl,
      grips,
      overlayHost: wrapEl,
      storageKey: STANDINGS_COL_PCTS_KEY,
      sizes: colPcts,
      unit: "pct",
      applySizes(sizes, ctx) {
        if (ctx) {
          const { deltaFrac, startSizes, gripIdx } = ctx;
          const dPct = deltaFrac * 100;
          let a = startSizes[gripIdx] + dPct;
          let b = startSizes[gripIdx + 1] - dPct;
          if (a < minPct) { b -= minPct - a; a = minPct; }
          if (b < minPct) { a -= minPct - b; b = minPct; }
          sizes[gripIdx] = a;
          sizes[gripIdx + 1] = b;
        }
        colEls.forEach((c, i) => { c.style.width = sizes[i] + "%"; });
      },
    });
    return el;
  }
  function makeScheduleBody() {
    const el = document.createElement("div");
    el.className = "wb-schedule";
    el.innerHTML = `<div class="wb-empty">Loading...</div>`;
    return el;
  }
  function makeLogBody() {
    const el = document.createElement("div");
    el.className = "wb-eventlog";
    el.innerHTML = `<div class="wb-error-banner" hidden></div><ul class="wb-eventlog-list"></ul>`;
    return el;
  }
  function makeEnginesBody() {
    const el = document.createElement("div");
    el.className = "wb-engines";
    el.innerHTML = `<div class="wb-empty">Loading...</div>`;
    return el;
  }

  // Renderers read these via the closure; reassigned when a window is
  // re-opened after the user closed it (so renderers target the new body).
  let standingsBody = makeStandingsBody();
  let scheduleBody = makeScheduleBody();
  let logBody = makeLogBody();
  let enginesBody = makeEnginesBody();

  // Per-window CSS class hooks (added to the WinBox outer container).
  // schedule = "Live Games" panel; gets a stable scrollbar gutter to
  // avoid width pulsation when rows come and go.
  const EXTRA_CLASS = {
    schedule: "sturddle-wb-live-games",
  };

  const LAYOUT_NAME = { 0: "NONE", 1: "TIDY", 2: "TILE", 3: "SNAP" };
  const OVERFLOW_X_OFFSET = 24;
  // Counts consecutive overflow restores in TIDY mode (grid full); reset on
  // successful slot claim so the cascade restarts from the left edge.
  let overflowRestoreCount = 0;

  function setShadow(wb, on) {
    wb.g?.classList.toggle("no-shadow", !on);
  }

  // pageX recorded on mousedown of a maximized window's header; used by onReflow
  // to reposition the window so the cursor stays proportionally on the title bar.
  let pendingDragX = null;

  // Shared onminimize/onrestore handler for all windows. Set once at creation;
  // reads activeLayout at call time so layout switches never leave stale handlers.
  const onReflow = (wb, isRestore) => {
    //console.log("[onReflow] layout=", LAYOUT_NAME[activeLayout], isRestore ? "restore" : "minimize");
    if (isRestore && pendingDragX !== null) {
      // Drag-unmaximize: place restored window so cursor stays at the same
      // proportional position on the title bar it occupied in the maximized window.
      const ratio = Math.max(0, Math.min(1, (pendingDragX - left) / (window.innerWidth - left)));
      const x = Math.max(left, Math.min(pendingDragX - Math.round(ratio * wb.width), window.innerWidth - wb.width));
      wb.move(x, wb.y);
      pendingDragX = null;
      return;
    }
    if (activeLayout === LAYOUT.TIDY) {
      // slotGrid only positions live (watcher) windows. System windows
      // (engines/standings/schedule/log) belong to the TIDY region grid
      // -- reflow the whole layout to place them correctly.
      if (isRestore) {
        if (!getLiveWindows().includes(wb)) {
          requestAnimationFrame(reapplyLayout);
          return;
        }
        const c = slotGrid.claim(wb);
        if (c) {
          wb._justRestored = true;
          wb.resize(c.w, c.h).move(c.x, c.y);
          setShadow(wb, false);
          overflowRestoreCount = 0;
        } else {
          const x = Math.min(
            left + overflowRestoreCount * OVERFLOW_X_OFFSET,
            Math.max(left, window.innerWidth - wb.width),
          );
          wb.move(x, top);
          setShadow(wb, true);
          overflowRestoreCount++;
        }
      }
    } else if (activeLayout === LAYOUT.TILE || activeLayout === LAYOUT.SNAP) {
      requestAnimationFrame(reapplyLayout);
    }
  };

  // Reflow + header wiring needed for any game window (live or frozen)
  // to participate in TIDY/TILE/SNAP layouts and slot-grid placement.
  function wireLayoutHandlers(wb) {
    wb.onminimize = () => onReflow(wb, false);
    wb.onrestore = () => onReflow(wb, true);
    wireHeader(wb);
  }

  // Wire the drag-unmaximize repositioning on a window's header.
  function wireHeader(wb) {
    const dragEl = wb.g?.querySelector(".wb-drag");
    if (!dragEl) return;
    dragEl.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      if (wb.max) {
        // Arm on first mousemove rather than mousedown: if WinBox restores via
        // dblclick the restore fires before the 2nd mousedown, so we can't know
        // on mousedown alone whether a drag or dblclick will follow.
        wb._armX = e.pageX;
        const onMove = () => {
          if (wb._armX !== null) {
            pendingDragX = wb._armX;
            wb._armX = null;
          }
        };
        window.addEventListener("mousemove", onMove, { once: true });
        window.addEventListener("mouseup", () => {
          window.removeEventListener("mousemove", onMove);
          wb._armX = null;
          pendingDragX = null;
        }, { once: true });
      } else if (!wb._justRestored) {
        // Any header mousedown (click or drag) shows shadow; reapplyLayout clears it.
        setShadow(wb, true);
      }
      wb._justRestored = false;
    }, { capture: true });
  }

  // Inset (px) from the right viewport edge to the workspace area --
  // i.e. ribbon width when docked right, 0 when docked left. WinBox's
  // maximize() respects this so a maximized window stops at the ribbon.
  const getRightInset = () => Math.max(0, window.innerWidth - getRight());

  function makeBox(key, title, body, { min = false, max = false } = {}) {
    const cfg = lastGeometry[key];
    const extra = EXTRA_CLASS[key] ? ` ${EXTRA_CLASS[key]}` : "";
    const wb = new WinBox({
      title, mount: body, top, left, right: getRightInset(), min, max,
      ...(cfg ? { x: cfg.x, y: cfg.y, width: cfg.width, height: cfg.height } : {}),
      class: `sturddle-wb no-full no-shadow${extra}`,
      ...MIN_SIZES[key],
    });
    // Stash so tile()/snap() can read the effective min size from the
    // instance (WinBox doesn't expose its config min* on the instance).
    wb.svMinWidth = MIN_SIZES[key].minwidth;
    wb.svMinHeight = MIN_SIZES[key].minheight;
    // Wire onclose after construction (TDZ on `wb` otherwise). No persist
    // here -- state is captured at workspace.close()/closeAll()/finalize().
    wb.onclose = () => {
      lastGeometry[key] = wbGeometry(wb);
      windows[key] = null;
      if (Object.values(windows).every((w) => w === null)) tearDown();
      else if (activeLayout !== LAYOUT.TIDY) requestAnimationFrame(reapplyLayout);
      return false;
    };
    wb.onminimize = () => onReflow(wb, false);
    wb.onrestore = () => onReflow(wb, true);
    wireHeader(wb);
    if (top > 0 && wb.y < top) wb.move(wb.x, top);
    if (left > 0 && wb.x < left) wb.move(left, wb.y);
    return wb;
  }

  function wbGeometry(wb) {
    return {
      x: `${Math.round(wb.x)}px`,
      y: `${Math.round(wb.y)}px`,
      width: `${Math.round(wb.width)}px`,
      height: `${Math.round(wb.height)}px`,
    };
  }

  function snapshotLive() {
    return getLiveWindows()
      .filter(wb => wb._watchOpts)
      .map(wb => {
        const resolved = wb._watchOpts.gameId ? resolvedGames.get(wb._watchOpts.gameId) : null;
        return {
          ...wb._watchOpts,
          ...wbGeometry(wb),
          min: !!wb.min, max: !!wb.max, z: wb.index ?? 0,
          ...(resolved ? { resolved } : {}),
        };
      });
  }

  function snapshot() {
    const state = {};
    for (const key of Object.keys(windows)) {
      const wb = windows[key];
      state[key] = wb
        ? { open: true, ...wbGeometry(wb), min: !!wb.min, max: !!wb.max, z: wb.index ?? 0 }
        : { open: false, ...lastGeometry[key], min: false, max: false, z: 0 };
    }
    state.live = snapshotLive();
    return state;
  }

  const windowSpecs = {
    standings: {
      title: "Standings",
      makeBody: makeStandingsBody,
      setBody: (b) => { standingsBody = b; },
      render: () => renderStandings(),
    },
    schedule: {
      title: "Live Games",
      makeBody: makeScheduleBody,
      setBody: (b) => { scheduleBody = b; },
      render: () => renderSchedule(),
    },
    engines: {
      title: "Engine Instances",
      makeBody: makeEnginesBody,
      setBody: (b) => { enginesBody = b; },
      render: () => renderEngines(),
    },
    log: {
      title: "Event log",
      makeBody: makeLogBody,
      setBody: (b) => { logBody = b; },
      render: () => renderEventLog(),
      postCreate: (wb) => {
        wb.addControl({
          class: "wb-log-copy-ctrl",
          index: 0,
          click: () => {
            const text = eventLog
              .filter(e => e.payload?.kind !== KIND.PROXY_UNPAIRED)
              .map(e => {
                const ts = e.ts || "";
                const inner = e.payload?.kind;
                const line = e.payload?.line;
                if (inner === KIND.RUNNER_LOG && line) return `${ts} ${line}`;
                const parts = [e.kind];
                if (inner) parts.push(inner);
                if (inner === KIND.PROXY_PAIRED)
                  parts.push(`${e.payload?.engine_a}(${e.payload?.proxy_a||""}) vs ${e.payload?.engine_b}(${e.payload?.proxy_b||""})`);
                else if (inner === KIND.GAME_FINISHED) {
                  const result = e.payload?.result;
                  const termination = e.payload?.termination;
                  const tail = (result && termination && termination !== "unknown")
                    ? `${result} ${termination}` : (result || "");
                  const gn = e.payload?.game_n;
                  if (gn != null) parts[parts.length - 1] = `${parts[parts.length - 1]} #${gn}`;
                  parts.push(`${e.payload?.engine_a} vs ${e.payload?.engine_b}`,
                             ...(tail ? [tail] : []));
                }
                return `${ts} ${parts.join(" ")}`;
              }).join("\n");
            navigator.clipboard.writeText(text)
              .then(() => toast("Event log copied to clipboard", { variant: "success", duration: 1500 }))
              .catch(() => {});
          },
        });
      },
    },
  };

  const windows = { standings: null, schedule: null, engines: null, log: null };
  if (restoreFromSaved) {
    // Recreate in saved z-order so the highest-z slot ends up topmost.
    const openKeys = Object.keys(windows)
      .filter(k => savedState[k]?.open)
      .sort((a, b) => (savedState[a].z ?? 0) - (savedState[b].z ?? 0));
    for (const key of openKeys) {
      const s = savedState[key];
      const spec = windowSpecs[key];
      const body = spec.makeBody();
      spec.setBody(body);
      windows[key] = makeBox(key, spec.title, body, { min: s.min, max: s.max });
      spec.postCreate?.(windows[key]);
    }
  } else {
    // Default: standings only; initWorkspace opens more based on tournament status.
    windows.standings = makeBox("standings", windowSpecs.standings.title, standingsBody);
  }
  requestAnimationFrame(reapplyLayout);

  // ---- Rendering --------------------------------------------------------

  function renderStandings() {
    const sprtSlot = standingsBody.querySelector(".wb-sprt-slot");
    const partialSlot = standingsBody.querySelector(".wb-partial-slot");
    const emptyEl = standingsBody.querySelector(".wb-standings-empty");
    const wrapEl = standingsBody.querySelector(".wb-standings-table-wrap");
    const tbody = standingsBody.querySelector(".wb-standings-tbl tbody");
    const standings = detail?.standings;
    if (!standings || standings.engines.length === 0) {
      emptyEl.textContent = "No games played yet.";
      emptyEl.hidden = false;
      wrapEl.hidden = true;
      sprtSlot.innerHTML = "";
      partialSlot.innerHTML = "";
      return;
    }
    emptyEl.hidden = true;
    wrapEl.hidden = false;
    const sprt = detail.sprt;
    tbody.innerHTML = standings.engines
      .map((e) => {
        const eloCell = e.elo == null
          ? "--"
          : (e.elo >= 0 ? "+" : "") + e.elo.toFixed(1) +
            (e.elo_margin_95 == null ? "" : ` +/- ${e.elo_margin_95.toFixed(1)}`);
        const ordoCell = e.elo_ordo == null
          ? "--"
          : (e.elo_ordo >= 0 ? "+" : "") + e.elo_ordo.toFixed(1) +
            (e.elo_ordo_margin_95 == null ? "" : ` +/- ${e.elo_ordo_margin_95.toFixed(1)}`);
        return `
        <tr>
          <td class="wb-eng-name">${escapeHtml(e.name)}</td>
          <td>${e.games}</td>
          <td>${e.wins}</td>
          <td>${e.losses}</td>
          <td>${e.draws}</td>
          <td>${e.points}</td>
          <td>${(e.score_pct * 100).toFixed(1)}%</td>
          <td>${eloCell}</td>
          <td>${ordoCell}</td>
        </tr>`;
      })
      .join("");
    if (sprt) {
      const lo = sprt.lower_bound, hi = sprt.upper_bound, llr = sprt.llr;
      const concluded = sprt.status !== "continue";
      const colorMod = concluded ? (sprt.status === "H1" ? " wb-sprt--h1" : " wb-sprt--h0") : "";
      const candidate = detail.engines?.[0]?.name ? escapeHtml(detail.engines[0].name) : "candidate";
      const pairsText = sprt.pairs != null ? ` * ${sprt.pairs} pair${sprt.pairs === 1 ? "" : "s"}` : "";
      const statusText = sprt.status === "H1"
        ? `H1 (${candidate} is stronger)`
        : sprt.status === "H0"
          ? `H0 (no significant difference)`
          : sprt.status;
      sprtSlot.innerHTML = `<div class="wb-sprt${colorMod}">` +
        `SPRT ${candidate} [${sprt.elo0}, ${sprt.elo1}] * LLR=${llr.toFixed(2)} [${lo.toFixed(2)}, ${hi.toFixed(2)}]` +
        `${pairsText} * ${statusText}` +
        `</div>`;
    } else {
      sprtSlot.innerHTML = "";
    }
    const partialPairs = detail.partial_pairs ?? 0;
    // Hide during RUNNING -- a fresh game-1 always sits alone in the
    // PGN until game-2 of the pair finishes; that's normal, not data loss.
    const showPartial = partialPairs > 0 && detail.status !== STATUS.RUNNING;
    partialSlot.innerHTML = showPartial
      ? `<div class="wb-partial-pairs">${partialPairs} incomplete pair${partialPairs === 1 ? "" : "s"} ` +
        `(one game missing)</div>`
      : "";
  }

  function attachWatch(btn, attachKey, sourceWindowKey, openOpts) {
    if (DEBUG_WATCH) console.log("[WATCH] click", { attachKey, sourceWindowKey, openOpts });
    // Slot grid is tidy-mode only; tile/snap reflow handles placement.
    const useSlotsGrid = activeLayout === LAYOUT.TIDY;
    // Claim a slot BEFORE creating the window so the new window's own
    // default position doesn't shadow the slot it would occupy.
    // Skip claim for windows being restored as minimized -- they dock, not slot.
    const rawClaim = (useSlotsGrid && !openOpts.min && !isLiveWindowOpen(attachKey)) ? slotGrid.claim() : null;
    // Clamp to viewport so a slot near the right/bottom edge can't
    // push the window off-screen.
    const claim = rawClaim ? {
      ...rawClaim,
      x: Math.min(rawClaim.x, Math.max(left, window.innerWidth - rawClaim.w)),
      y: Math.min(rawClaim.y, Math.max(top, window.innerHeight - rawClaim.h)),
    } : null;
    let result;
    try {
      result = openLiveGameWindow({
        ...openOpts, token, tournamentId: tournament.id,
        top, left, right: getRightInset(),
        boardStyle: boardStyleCached,
        initialRect: claim ? { x: claim.x, y: claim.y, w: claim.w, h: claim.h } : (openOpts.initialRect ?? null),
      });
    } catch (e) {
      console.error("[WATCH] openLiveGameWindow threw", e, { attachKey, openOpts });
      return;
    }
    // No slot fit in tidy/none mode -- minimize so the grid stays clean.
    if (result?.wb && !result.alreadyOpen && useSlotsGrid && !claim) {
      try { result.wb.minimize(); } catch { /* */ }
    }
    if (result?.wb && !result.alreadyOpen && !result.wb.min) requestAnimationFrame(reapplyLayout);
    if (result?.wb && !result.alreadyOpen) wireLayoutHandlers(result.wb);
    const isLive = isLiveWindowOpen(attachKey);
    if (DEBUG_WATCH) console.log("[WATCH] post-open", { attachKey, isLive, slotted: !!claim });
    btn?.classList.toggle("wb-sched-attach-btn--live", isLive);
  }

  function renderSchedule() {
    if (livePairings.size === 0) {
      // Pair confirmation can lag game start by seconds at fast tc;
      // distinguish "settling" (proxies up, no confirmed pairs yet)
      // from "really nothing running".
      const settling = (
        detail?.status === STATUS.RUNNING && activeProxies.size > 0
      );
      scheduleBody.innerHTML = settling
        ? `<div class="wb-empty">Starting up...</div>`
        : `<div class="wb-empty">No games in play.</div>`;
      return;
    }
    const scroller = scheduleBody.parentElement;
    const atBottom = !scroller || isPinnedToBottom(scroller, AUTOSCROLL_SLACK_ROW_PX);
    scheduleBody.innerHTML = `<ul class="wb-sched-list"></ul>`;
    const list = scheduleBody.querySelector(".wb-sched-list");

    // Dedupe: both proxies map to the same info object,
    // so skip if we already rendered this pair.
    const shownPairs = new Set();
    for (const [, info] of livePairings) {
      const key = [info.proxyA, info.proxyB].sort().join(":");
      if (shownPairs.has(key)) continue;
      shownPairs.add(key);
      const li = document.createElement("li");
      li.className = "wb-sched-live wb-sched-pair";
      const wLabel = info.sideA === "white" ? info.engineA : info.engineB;
      const bLabel = info.sideA === "white" ? info.engineB : info.engineA;
      li.innerHTML = `
        <span class="wb-sched-icon">&#9822;</span>
        <span class="wb-sched-game">${escapeHtml(wLabel)} - ${escapeHtml(bLabel)}</span>
      `;
      const btn = document.createElement("button");
      btn.className = "wb-sched-attach-btn";
      btn.textContent = "watch";
      btn.title = info.pairId || key;
      btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(info.pairId || key));
      btn.addEventListener("click", () => attachWatch(btn, info.pairId || key, "schedule", {
        proxyId: info.proxyA,
        gameId: info.pairId || null,
        label: `${wLabel} vs ${bLabel}`,
        engineName: wLabel,
      }));
      li.appendChild(btn);
      list.appendChild(li);
    }

    if (atBottom) scrollToBottom(scroller);
  }

  function renderEngines() {
    // One row per active proxy (engine process). Attach via proxy_id WS,
    // single-engine identity (survives book-line ambiguity where pair
    // confirmation hasn't happened yet). Distinct from Live Games which
    // is keyed on confirmed pair_ids.
    if (activeProxies.size === 0) {
      enginesBody.innerHTML = `<div class="wb-empty">No active engines.</div>`;
      return;
    }
    const scroller = enginesBody.parentElement;
    const atBottom = !scroller || isPinnedToBottom(scroller, AUTOSCROLL_SLACK_ROW_PX);
    enginesBody.innerHTML = `<ul class="wb-sched-list"></ul>`;
    const list = enginesBody.querySelector(".wb-sched-list");
    for (const [pid, p] of activeProxies) {
      const li = document.createElement("li");
      li.className = "wb-sched-live";
      const engineLabel = p.engineName || pid;
      li.innerHTML = `
        <span class="wb-sched-icon">&#9881;</span>
        <span class="wb-sched-game">${escapeHtml(engineLabel)}</span>
      `;
      const btn = document.createElement("button");
      btn.className = "wb-sched-attach-btn";
      btn.textContent = "watch";
      btn.title = pid;
      btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(pid));
      btn.addEventListener("click", () => attachWatch(btn, pid, "engines", {
        proxyId: pid,
        label: `${engineLabel}`,
        engineName: engineLabel,
      }));
      li.appendChild(btn);
      list.appendChild(li);
    }
    if (atBottom) scrollToBottom(scroller);
  }

  let _schedulePending = false;
  function scheduleSchedule() {
    if (_schedulePending) return;
    _schedulePending = true;
    requestAnimationFrame(() => { _schedulePending = false; renderSchedule(); });
  }

  let _enginesPending = false;
  function scheduleEngines() {
    if (_enginesPending) return;
    _enginesPending = true;
    requestAnimationFrame(() => { _enginesPending = false; renderEngines(); });
  }

  // rAF-coalesced render: at fast TC the runner_log stream can drive
  // hundreds of renders/sec; without this the main thread wedges and
  // button clicks feel dead.
  let _eventLogPending = false;
  function scheduleEventLog() {
    if (_eventLogPending) return;
    _eventLogPending = true;
    requestAnimationFrame(() => {
      _eventLogPending = false;
      renderEventLog();
    });
  }

  function renderEventLog() {
    const banner = logBody.querySelector(".wb-error-banner");
    if (banner) {
      const err = detail?.last_error;
      if (err) {
        // Server-emitted server-crash hint vs fastchess's own runner-crash
        // hint. Both produce a Restart button in the banner; we just need
        // to strip the source-side "press Start" prose so we don't show it
        // twice (once as text, once as the button).
        const RESTART_HINT_SUFFIX = "press Start to restart from scratch (prior games will be discarded).";
        const FASTCHESS_RESUME_LINE = "To resume the tournament, run:";
        const rawTail = (err.stderr_tail || []).slice(-10);
        const lastIdx = rawTail.length - 1;

        let displayLines = rawTail;
        let restartPre = null;
        if (lastIdx >= 0 && rawTail[lastIdx].includes(RESTART_HINT_SUFFIX)) {
          const head = rawTail[lastIdx].slice(0, rawTail[lastIdx].indexOf(RESTART_HINT_SUFFIX));
          const trimmed = head.replace(/[;\s]+$/, "");
          displayLines = trimmed
            ? [...rawTail.slice(0, lastIdx), trimmed]
            : rawTail.slice(0, lastIdx);
          restartPre = "; press ";
        } else {
          const i = rawTail.findIndex((l) => l.includes(FASTCHESS_RESUME_LINE));
          if (i >= 0) {
            displayLines = rawTail.slice(0, i);
            restartPre = " -- press ";
          }
        }
        // FAILED always means "won't resume itself", so Restart is
        // always a valid action. Default unconditionally so an
        // unrecognized failure shape doesn't dead-end the workspace.
        if (restartPre === null) restartPre = "; press ";

        const tail = displayLines.join("\n") || `exit code ${err.rc}`;
        banner.innerHTML = `<div class="wb-error-title">Tournament failed (rc=${err.rc})</div><pre>${escapeHtml(tail)}</pre>`;
        if (restartPre) {
          const blocked = otherActiveId != null;
          const otherLabel = otherActiveName ? `"${otherActiveName}"` : "another tournament";
          const tooltip = blocked
            ? `${otherLabel} is currently running. Stop it first.`
            : "Restart";
          const trailing = blocked
            ? ` -- stop ${otherLabel} first to restart.`
            : " to restart from scratch.";
          const pre = banner.querySelector("pre");
          pre.append(restartPre);
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "toast-icon-btn";
          btn.setAttribute("aria-label", "Restart");
          btn.setAttribute("title", tooltip);
          btn.disabled = blocked;
          const ic = document.createElement("wa-icon");
          ic.setAttribute("name", "rotate-right");
          btn.appendChild(ic);
          btn.addEventListener("click", async () => {
            // Re-read otherActiveId at click time. btn.disabled is
            // latched at render and can lag a setOtherActive update.
            if (otherActiveId != null) {
              const lbl = otherActiveName ? `"${otherActiveName}"` : "another tournament";
              toast(`${lbl} is currently running. Stop it first.`, { variant: "warning" });
              return;
            }
            const ok = await confirm(buildRestartConfirm(tournament.name, detail?.standings?.games ?? 0));
            if (!ok) return;
            try {
              await api("POST", `/api/tournaments/${tournament.id}/start?${CONFIRM_WIPE_QS}`);
            } catch (e) {
              toast(`Restart failed: ${apiErrorDetail(e)}`, { variant: "danger" });
            }
          });
          pre.appendChild(btn);
          pre.append(trailing);
        }
        banner.hidden = false;
      } else {
        banner.hidden = true;
        banner.innerHTML = "";
      }
    }
    const list = logBody.querySelector(".wb-eventlog-list");
    if (!list) return;
    const scroller = logBody.parentElement;
    const atBottom = !scroller || isPinnedToBottom(scroller, AUTOSCROLL_SLACK_ROW_PX);
    list.innerHTML = eventLog.filter(e => e.payload?.kind !== KIND.PROXY_UNPAIRED).map((e) => {
      const ts = e.ts || "";
      const inner = e.payload?.kind;
      // runner_log: surface the actual fastchess stdout/stderr line.
      if (inner === KIND.RUNNER_LOG && e.payload?.line) {
        const stream = e.payload.stream === "err" ? " err" : "";
        return `<li><span class="wb-log-ts">${ts}</span>` +
          `<span class="wb-log-runner${stream}">${escapeHtml(e.payload.line)}</span></li>`;
      }
      // Muted detail parts appended after the primary kind label.
      const parts = [];
      if (e.kind === EVT.STATUS && e.payload?.status)
        parts.push(e.payload.status);
      else if (inner === KIND.GAME_FINISHED) {
        const a = e.payload?.engine_a || "?";
        const b = e.payload?.engine_b || "?";
        const result = e.payload?.result;
        const termination = e.payload?.termination;
        const tail = (result && termination && termination !== "unknown")
          ? `${result} ${termination}` : (result || "");
        const gn = e.payload?.game_n;
        const head = (gn != null) ? `${inner} #${gn}` : inner;
        parts.push(head, `${a} vs ${b}`, ...(tail ? [tail] : []));
      } else if (inner === KIND.PROXY_PAIRED) {
        const a = e.payload?.engine_a || "?";
        const b = e.payload?.engine_b || "?";
        const pa = e.payload?.proxy_a || "";
        const pb = e.payload?.proxy_b || "";
        parts.push(inner, `${a}(${pa}) vs ${b}(${pb})`);
      } else if (inner === KIND.PROXY_UNPAIRED) {
        const pa = e.payload?.proxy_id || "";
        const pb = e.payload?.peer_id  || "";
        parts.push(inner, pa, pb);
      } else if (inner === KIND.PROXY_STARTED) {
        parts.push(inner);
        if (e.payload?.engine_name) parts.push(e.payload.engine_name);
      } else if (inner === KIND.RUNNER_CRASH) {
        parts.push(inner);
        if (e.payload?.rc != null) parts.push(`rc=${e.payload.rc}`);
      } else if (inner) {
        parts.push(inner);
      }
      const detailHtml = parts.map(p => ` <span class="wb-log-detail">${escapeHtml(p)}</span>`).join("");
      return `<li><span class="wb-log-ts">${ts}</span> <span class="wb-log-kind">${escapeHtml(e.kind)}</span>${detailHtml}</li>`;
    }).join("");
    if (atBottom) scrollToBottom(scroller);
  }

  // ---- Data refresh -----------------------------------------------------

  async function refresh() {
    let fresh;
    try {
      fresh = await api("GET", `/api/tournaments/${tournament.id}`);
    } catch (e) {
      log?.(`workspace refresh failed: ${e.message}`);
      return;
    }
    applyDetail(fresh);
  }

  // Tracks any OTHER tournament currently running. Drives the failure
  // banner's Restart disabled state -- restarting while another holds
  // the single-active slot would 409 after a confirmed wipe.
  let otherActiveId = null;
  let otherActiveName = null;
  function setOtherActive(id, name) {
    const nextId = (id && id !== tournament.id) ? id : null;
    const nextName = nextId ? (name || null) : null;
    if (nextId === otherActiveId && nextName === otherActiveName) return;
    otherActiveId = nextId;
    otherActiveName = nextName;
    renderEventLog();
  }

  function applyDetail(fresh) {
    detail = fresh;
    // While RUNNING, API state can lag WS events under fast tc, so
    // treat API as additive (add missing entries, never remove).
    // When not RUNNING, replace authoritatively to drop ghosts.
    const seededProxies = detail.proxies_active || [];
    if (detail.status !== STATUS.RUNNING) activeProxies.clear();
    for (const p of seededProxies) {
      if (p.proxy_id && !activeProxies.has(p.proxy_id)) {
        activeProxies.set(p.proxy_id, { engineName: p.engine_name || null });
      }
    }
    const seededPairings = detail.pairings_active || [];
    if (detail.status !== STATUS.RUNNING) livePairings.clear();
    for (const p of seededPairings) {
      if (livePairings.has(p.proxy_a) || livePairings.has(p.proxy_b)) continue;
      const info = { pairId: p.pair_id, proxyA: p.proxy_a, engineA: p.engine_a, sideA: p.side_a,
                     proxyB: p.proxy_b, engineB: p.engine_b, sideB: p.side_b };
      livePairings.set(p.proxy_a, info);
      livePairings.set(p.proxy_b, info);
    }
    renderStandings();
    renderSchedule();
    renderEngines();
    renderEventLog();
  }

  function addLogEntry(evt) {
    const seq = evt.payload?._seq;
    // `game_reconciled` doesn't go into eventLog (it upgrades a prior
    // game_finished row in pushEvent); duplicates are idempotent there
    // so we don't need to track its seq at all.
    if (evt.payload?.kind === KIND.GAME_RECONCILED) return false;
    if (seq != null) {
      if (seenSeqs.has(seq)) return false;
      seenSeqs.add(seq);
    }
    const tsRaw = evt.payload?._ts;
    const ts = (tsRaw ? new Date(tsRaw) : new Date())
      .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    eventLog.push({ ts, kind: evt.kind, payload: evt.payload, _seq: seq });
    // Keep ordered by seq so backfill items slot in before any live
    // events that arrived during the REST round-trip.
    eventLog.sort((a, b) => (a._seq ?? 0) - (b._seq ?? 0));
    // Cap eventLog and keep seenSeqs in lockstep so it can't outgrow
    // the visible log -- the dedup only needs to cover items we'd
    // otherwise re-render.
    while (eventLog.length > EVENT_LOG_LIMIT) {
      const evicted = eventLog.shift();
      if (evicted?._seq != null) seenSeqs.delete(evicted._seq);
    }
    return true;
  }

  function pushEvent(evt) {
    if (!evt) return;
    if (!evt.kind?.startsWith(EVT_PREFIX)) return;
    // Only events for *our* tournament -- the orchestrator stamps
    // tournament_id into payloads on the server side.
    const tid = evt.payload?.tournament_id;
    if (tid && tid !== tournament.id) return;

    const added = addLogEntry(evt);

    // Track active proxies for Schedule rows.
    const inner = evt.payload?.kind;
    if (inner === KIND.PROXY_STARTED) {
      const pid = evt.payload.proxy_id;
      if (pid) {
        activeProxies.set(pid, {
          engineName: evt.payload.engine_name || null,
        });
      }
    } else if (inner === KIND.PROXY_ENDED) {
      const pid = evt.payload.proxy_id;
      if (pid) activeProxies.delete(pid);
    } else if (inner === KIND.PROXY_PAIRED) {
      const p = evt.payload;
      const info = { pairId: p.pair_id, proxyA: p.proxy_a, engineA: p.engine_a, sideA: p.side_a,
                     proxyB: p.proxy_b, engineB: p.engine_b, sideB: p.side_b };
      livePairings.set(p.proxy_a, info);
      livePairings.set(p.proxy_b, info);
    } else if (inner === KIND.GAME_FINISHED) {
      // Authoritative game-end signal -- drives livePairings cleanup +
      // Schedule re-render.
      livePairings.delete(evt.payload.proxy_a);
      livePairings.delete(evt.payload.proxy_b);
    } else if (inner === KIND.GAME_RECONCILED) {
      // Upgrade the prior `game_finished` entry for this pair_id with
      // the matched result/termination/game_n instead of pushing a
      // separate row. One game = one log entry.
      const pid = evt.payload?.pair_id;
      if (pid) {
        if (evt.payload.game_n != null) {
          resolvedGames.set(pid, {
            gameN: evt.payload.game_n,
            result: evt.payload.result,
            termination: evt.payload.termination,
          });
        }
        for (let i = eventLog.length - 1; i >= 0; i--) {
          const ent = eventLog[i];
          if (ent.payload?.kind === KIND.GAME_FINISHED && ent.payload?.pair_id === pid) {
            ent.payload = {
              ...ent.payload,
              result: evt.payload.result,
              termination: evt.payload.termination,
              game_n: evt.payload.game_n,
              reconciled: true,
            };
            break;
          }
        }
        // Live windows listening for this pair_id will repaint
        // their banner; no-op if window already closed.
        window.dispatchEvent(new CustomEvent("sturddle:reconciled", {
          detail: {
            pairId: pid,
            result: evt.payload.result,
            termination: evt.payload.termination,
            gameN: evt.payload.game_n ?? null,
            tournamentId: tournament.id,
          },
        }));
      }
    } else if (
      inner === KIND.DONE || inner === KIND.STOPPED ||
      (evt.kind === EVT.STATUS &&
       [STATUS.STOPPED, STATUS.DONE, STATUS.FAILED].includes(evt.payload?.status))
    ) {
      activeProxies.clear();
    }

    if (added) scheduleEventLog();
    if (inner === KIND.PROXY_STARTED || inner === KIND.PROXY_ENDED ||
        inner === KIND.PROXY_PAIRED || inner === KIND.PROXY_UNPAIRED ||
        inner === KIND.GAME_FINISHED || evt.kind === EVT.STATUS ||
        inner === KIND.DONE || inner === KIND.STOPPED)
      scheduleSchedule();
    if (inner === KIND.PROXY_STARTED || inner === KIND.PROXY_ENDED ||
        evt.kind === EVT.STATUS || inner === KIND.DONE || inner === KIND.STOPPED)
      scheduleEngines();

    // Status changes and game finishes are good triggers to refresh
    // standings authoritatively.
    if (
      evt.kind === EVT.STATUS ||
      inner === KIND.GAME_FINISHED ||
      inner === KIND.DONE ||
      inner === KIND.STOPPED
    ) {
      refresh();
    }

    if (
      evt.kind === EVT.STATUS &&
      [STATUS.STOPPED, STATUS.FAILED].includes(evt.payload?.status)
    ) {
      closeAllLiveGames();
      if (evt.payload.status === STATUS.STOPPED) {
        toast(`"${tournament.name}" stopped`, { variant: "warning" });
      }
    }
    // Tournament started: auto-open Live Games so the user sees
    // pairings as they form. Skip if restoring a saved desktop state --
    // the user may have intentionally closed that window.
    if (
      !restoreFromSaved &&
      evt.kind === EVT.STATUS &&
      evt.payload?.status === STATUS.RUNNING &&
      windows.schedule == null
    ) {
      openSystemWindow("schedule");
    }
  }

  function armSubscriptions() {
    if (unsubscribe == null) {
      unsubscribe = events.on(pushEvent);
    }
  }

  // Subscribe before backfill so any events firing during the REST
  // round-trip are captured (deduped against backfill via _seq).
  unsubscribe = events.on(pushEvent);

  async function backfillEvents() {
    try {
      const res = await api("GET", `/api/tournaments/${tournament.id}/events`);
      let added = false;
      for (const e of (res.events || [])) {
        if (!e.kind?.startsWith(EVT_PREFIX)) continue;
        if (addLogEntry(e)) added = true;
      }
      if (added) scheduleEventLog();
    } catch (e) {
      log?.(`event backfill failed: ${e.message}`);
    }
  }
  async function initWorkspace() {
    await Promise.all([refresh(), backfillEvents()]);
    if (!restoreFromSaved) {
      if (detail?.status === STATUS.RUNNING) openSystemWindow("schedule", { flash: false });
      if (eventLog.length > 0 || detail?.status === STATUS.RUNNING) openSystemWindow("log", { flash: false });
    }
    if (Array.isArray(savedState?.live) && savedState.live.length > 0) {
      const sorted = [...savedState.live].sort((a, b) => (a.z ?? 0) - (b.z ?? 0));
      const running = detail?.status === STATUS.RUNNING;
      let endedWhileAway = 0;
      for (const s of sorted) {
        // Don't restore minimized-window geometry -- it's the dock
        // position, not the pre-minimize rect.
        const rect = s.min ? null : { x: s.x, y: s.y, w: s.width, h: s.height };
        if (s.resolved) {
          // Resolved at snapshot time -- rehydrate from PGN, no WS.
          // Seed resolvedGames so subsequent snapshotLive() calls can
          // re-persist `resolved` (frozen windows don't produce live
          // game_reconciled events that would refill the map).
          if (s.gameId) resolvedGames.set(s.gameId, s.resolved);
          const fres = openFrozenGameWindow({
            proxyId: s.proxyId, gameId: s.gameId,
            label: s.label, engineName: s.engineName,
            token, tournamentId: tournament.id,
            gameN: s.resolved.gameN,
            result: s.resolved.result,
            termination: s.resolved.termination,
            top, left, right: getRightInset(),
            boardStyle: boardStyleCached,
            initialRect: rect, min: !!s.min, flash: false,
          });
          if (fres?.wb && !fres.alreadyOpen) wireLayoutHandlers(fres.wb);
        } else if (running) {
          // Live-reattach only if the server still considers this pair
          // alive; otherwise the WS would auto-close on first {ended}
          // and the user would see a window flash and vanish.
          const stillLive = s.gameId
            ? livePairings.get(s.proxyId)?.pairId === s.gameId
            : activeProxies.has(s.proxyId);
          if (stillLive) {
            attachWatch(null, s.gameId ?? s.proxyId, null, {
              proxyId: s.proxyId, gameId: s.gameId ?? null,
              label: s.label, engineName: s.engineName,
              initialRect: rect,
              min: !!s.min, flash: false,
            });
          } else {
            endedWhileAway++;
          }
        }
        else {
          // Not resolved, not running -- game ended while we had no
          // way to capture its resolution. Drop, count for toast.
          endedWhileAway++;
        }
      }
      if (endedWhileAway > 0) {
        const msg = endedWhileAway === 1
          ? "1 watched game finished while away."
          : `${endedWhileAway} watched games finished while away.`;
        toast(msg, { variant: "warning", duration: 7000 });
      }
      requestAnimationFrame(reapplyLayout);
    }
  }
  initWorkspace();

  // Periodic refresh is driven by the tournaments-list poll (single source);
  // it calls refresh() on this workspace handle. Catches WS gaps + PGN-only
  // changes (games_played advancing without the tailer subscribed).
  function onReconnect(e) {
    if (!e.detail?.connected) {
      seenSeqs.clear();
      eventLog.length = 0;
      return;
    }
    refresh();
    backfillEvents();
  }
  const onBeforeUnload = () => saveState(tournament.id, snapshot());
  window.addEventListener("beforeunload", onBeforeUnload);
  window.addEventListener("sturddle:connection", onReconnect);
  window.addEventListener("sturddle:livegame-closed", refreshWatchButtons);
  const onLiveGameClosedReapply = () => { if (activeLayout !== LAYOUT.TIDY) requestAnimationFrame(reapplyLayout); };
  window.addEventListener("sturddle:livegame-closed", onLiveGameClosedReapply);

  let resizeTimer = null;
  const onResize = () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const all = openWindows();
      const anyMax = all.some(wb => wb.max);
      for (const wb of all) if (wb.max) { wb.restore(); wb.maximize(); }
      if (!anyMax) {
        if (activeLayout === LAYOUT.TIDY) tidy({ preserveMin: true });
        else if (activeLayout === LAYOUT.TILE) tile(null, { preserveMin: true, reserveDock: true });
        else if (activeLayout === LAYOUT.SNAP) snap();
      }
    }, 150);
  };
  function attachResizeListeners() {
    window.addEventListener("resize", onResize);
    document.addEventListener("fullscreenchange", onResize);
  }
  function detachResizeListeners() {
    window.removeEventListener("resize", onResize);
    document.removeEventListener("fullscreenchange", onResize);
    clearTimeout(resizeTimer);
  }
  attachResizeListeners();

  // ---- Tear-down --------------------------------------------------------

  let liveWatcherAttached = false;

  function refreshWatchButtons() {
    for (const body of [scheduleBody, enginesBody]) {
      for (const btn of body.querySelectorAll(".wb-sched-attach-btn")) {
        btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(btn.title));
      }
    }
  }

  function onLiveGameClosed() {
    const allStandardClosed = Object.values(windows).every((w) => w === null);
    if (allStandardClosed && getLiveWindows().length === 0) finalize();
  }

  function finalize() {
    if (finalized) return;
    finalized = true;
    window.removeEventListener("beforeunload", onBeforeUnload);
    window.removeEventListener("sturddle:connection", onReconnect);
    window.removeEventListener("sturddle:livegame-closed", refreshWatchButtons);
    window.removeEventListener("sturddle:livegame-closed", onLiveGameClosedReapply);
    detachResizeListeners();
    if (liveWatcherAttached) {
      window.removeEventListener("sturddle:livegame-closed", onLiveGameClosed);
      liveWatcherAttached = false;
    }
    // User X-closed the last window: persist a dismissed snapshot so a
    // future navigation does not auto-reopen the workspace.
    if (!explicitlyClosed) {
      saveState(tournament.id, { ...snapshot(), _closed: true });
    }
    if (activeWorkspace === workspace) activeWorkspace = null;
    // Re-sync the Window menu (the user may have closed via X, not the menu).
    window.dispatchEvent(new CustomEvent("sturddle:workspace-closed"));
  }

  function tearDown() {
    if (unsubscribe) {
      unsubscribe();
      unsubscribe = null;
    }
    // While live-game windows survive, keep the workspace "active" so the
    // Window menu can still operate on them. Defer finalize until the last
    // live window closes.
    if (getLiveWindows().length > 0) {
      if (!liveWatcherAttached) {
        window.addEventListener("sturddle:livegame-closed", onLiveGameClosed);
        liveWatcherAttached = true;
      }
      return;
    }
    finalize();
  }

  // Snapshot, mark explicit-close, force-close all standard windows,
  // tear down. Used by both close() (navigate-away) and closeAll().
  function dismissWindows({ markClosed, liveSnap } = {}) {
    const state = snapshot();
    if (liveSnap) state.live = liveSnap;
    if (markClosed) state._closed = true;
    saveState(tournament.id, state);
    explicitlyClosed = true;
    for (const k of Object.keys(windows)) {
      if (windows[k]) {
        windows[k].close(true);
        windows[k] = null;
      }
    }
    closeAllLiveGames();
    tearDown();
  }

  // Tournament-switch path: snapshot stays restorable (no _closed). Stale
  // live windows close; resolved game-id windows persist (final banner).
  // tearDown() defers finalize until those finally close.
  function close() {
    dismissWindows({ markClosed: false });
  }

  function openWindows() {
    return [...Object.values(windows).filter(Boolean), ...getLiveWindows()];
  }

  function unminimize(wb) {
    // resize/move on a minimized or maximized WinBox leaves it stuck in
    // that state -- restore first so the new geometry actually takes.
    if (wb.min || wb.max) wb.restore();
  }

  // Focus windows top-left first so bottom-right ends up on top.
  function zOrder(wbs) {
    [...wbs].sort((a, b) => a.y !== b.y ? a.y - b.y : a.x - b.x)
      .forEach(wb => { try { wb.focus(); } catch { /* */ } });
  }

  // Reserved strip at the bottom so minimized WinBoxes have a place to dock.
  const MINIMIZE_FOOTER_H = 36;
  // Visual gap between tiled/snapped windows; also absorbs WinBox rounding.
  const TILE_MARGIN = 0;
  const TIDY_GAP = 0;

  // wbsIn: explicit list (snap fallback -- skip minimized, don't unminimize).
  function tile(wbsIn, { reserveDock = false, preserveMin = false } = {}) {
    setLayout(LAYOUT.TILE);
    let wbs = wbsIn ?? openWindows();
    if (!wbs.length) return;
    if (preserveMin) wbs = wbs.filter(wb => !wb.min);
    else wbs.forEach(unminimize);
    const availW = getRight() - left;
    const availH = window.innerHeight - top - (reserveDock ? MINIMIZE_FOOTER_H : 0);
    const maxMinW = Math.max(...wbs.map(wb => wb.svMinWidth ?? 0));
    const maxMinH = Math.max(...wbs.map(wb => wb.svMinHeight ?? 0));
    const n = wbs.length;
    const maxCols = maxMinW ? Math.floor(availW / maxMinW) : n;
    const minCols = maxMinH ? Math.ceil(n / Math.max(1, Math.floor(availH / maxMinH))) : 1;
    const cols = Math.min(maxCols, Math.max(minCols, Math.ceil(Math.sqrt(n))));
    const rows = Math.ceil(n / cols);
    const w = Math.floor(availW / cols);
    const h = Math.floor(availH / rows);
    wbs.forEach((wb, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      // Window size = cell minus TILE_MARGIN, but floored to per-window min
      // (live-game windows stash svMinWidth/svMinHeight at creation; WinBox
      // doesn't expose config min* on the instance).
      const ww = Math.max(w - TILE_MARGIN, wb.svMinWidth ?? 0);
      const hh = Math.max(h - TILE_MARGIN, wb.svMinHeight ?? 0);
      // Clamp position so bottom-right stays inside [availW, availH] when
      // min size > cell size -- prevents the bottom row from spilling into
      // the reserved dock area. Trade-off: pushed windows may overlap the
      // row/column above them. Acceptable for this "didn't fit" fallback.
      const x = Math.max(left, Math.min(left + col * w, left + availW - ww));
      const y = Math.max(top,  Math.min(top  + row * h, top  + availH - hh));
      wb.resize(ww, hh).move(x, y);
      setShadow(wb, false);
    });
    zOrder(wbs);
  }

  // 2x2 in the bottom half of the viewport. Auto-opens any of the
  // four target windows that aren't open yet.
  function tidy({ preserveMin = false } = {}) {
    setLayout(LAYOUT.TIDY);
    const keys = ["engines", "standings", "schedule", "log"];
    for (const k of keys) {
      if (!windows[k]) openSystemWindow(k, { flash: false });
    }
    // If watchers exist, re-grid them first while the 4 system panels
    // are hidden, so the user doesn't see the panels flicker beneath
    // the watcher reshuffle. Overflow watchers are minimized.
    const watchers = getLiveWindows();
    if (watchers.length > 0) {
      for (const k of keys) {
        const wb = windows[k];
        if (wb) try { wb.hide(); } catch { /* */ }
      }
      const cap = slotGrid.capacity();
      let slot = 0;
      // Sort by current x position so slot assignment matches physical order,
      // not registration order. Minimized/maximized go last when preserving.
      const ordered = [...watchers].sort((a, b) => {
        const aDeferred = preserveMin && (a.min || a.max);
        const bDeferred = preserveMin && (b.min || b.max);
        if (aDeferred !== bDeferred) return aDeferred ? 1 : -1;
        return a.x !== b.x ? a.x - b.x : a.y - b.y;
      });
      ordered.forEach(wb => {
        if (preserveMin && (wb.min || wb.max)) {
          return; // onReflow handles slot placement on restore
        }
        if (slot < cap) {
          unminimize(wb);
          const r = slotGrid.rectAt(slot++);
          wb.resize(r.w, r.h).move(r.x, r.y);
          setShadow(wb, false);
        } else {
          try { wb.minimize(); } catch { /* */ }
          setShadow(wb, true);
        }
      });
      for (const k of keys) {
        const wb = windows[k];
        if (wb) try { wb.show(); } catch { /* */ }
      }
    }
    const availW = getRight() - left;
    const availH = window.innerHeight - top - MINIMIZE_FOOTER_H;
    const leftW = Math.max(Math.round(availW * 0.35), MIN_SIZES.engines.minwidth);
    const rightW = availW - leftW - TIDY_GAP;
    // System rows get what's left after one row of board slots.
    // Clamp each row so both windows in a row share the same height
    // (WinBox silently floors to per-window minheight otherwise).
    const systemH = availH - LIVE_MIN_HEIGHT;
    const desiredRowH = Math.floor((systemH - TIDY_GAP) / 2);
    const topRowH = Math.max(desiredRowH, MIN_SIZES.engines.minheight, MIN_SIZES.standings.minheight);
    const botRowH = Math.max(desiredRowH, MIN_SIZES.schedule.minheight, MIN_SIZES.log.minheight);
    // Anchor bottom edge to top + availH (which already excludes the
    // minimize footer). If clamped rows exceed availH the layout
    // extends upward, but never below the reserved footer.
    const regionTop = top + availH - (topRowH + TIDY_GAP + botRowH);
    const placements = [
      ["engines",   left,                   regionTop,                       leftW,  topRowH],
      ["standings", left + leftW + TIDY_GAP, regionTop,                      rightW, topRowH],
      ["schedule",  left,                   regionTop + topRowH + TIDY_GAP,  leftW,  botRowH],
      ["log",       left + leftW + TIDY_GAP, regionTop + topRowH + TIDY_GAP, rightW, botRowH],
    ];
    for (const [k, x, y, w, h] of placements) {
      const wb = windows[k];
      if (!wb) continue;
      unminimize(wb);
      wb.resize(w, h).move(x, y);
      setShadow(wb, false);
    }
    if (!preserveMin) zOrder(openWindows().filter(wb => !wb.min));
  }

  // Snap: k-d tree / slice-and-dice partition. Recursively split the
  // viewport at the axis of greatest center-spread; each leaf gets one
  // window. Produces a perfect rectangular tiling -- no gaps, no overlaps,
  // O(N log N), idempotent. Minimized/maximized windows are skipped.
  function snap() {
    setLayout(LAYOUT.SNAP);
    const vx0 = left, vy0 = top;
    const vx1 = getRight();

    const allWindows = openWindows();
    // Restore any maximized windows so they participate in the snap layout
    // (otherwise non-max windows would be tiled invisibly underneath them).
    // Minimized windows stay minimized and are excluded.
    for (const wb of allWindows) if (wb.max) wb.restore();
    const wbs = allWindows.filter(wb => !wb.min);
    if (!wbs.length) return;
    // Reserve bottom strip for the minimize dock only if any window is
    // currently minimized -- otherwise full viewport.
    const hasMin = allWindows.some(wb => wb.min);
    const vy1 = window.innerHeight - (hasMin ? MINIMIZE_FOOTER_H : 0);

    const items = wbs.map(wb => ({
      wb,
      cx: wb.x + wb.width / 2,
      cy: wb.y + wb.height / 2,
      cw: wb.width,
      ch: wb.height,
      minW: wb.svMinWidth ?? 1,
      minH: wb.svMinHeight ?? 1,
      rect: null,
    }));

    function partition(rect, group) {
      if (group.length === 1) { group[0].rect = rect; return; }
      let xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
      for (const it of group) {
        if (it.cx < xMin) xMin = it.cx;
        if (it.cx > xMax) xMax = it.cx;
        if (it.cy < yMin) yMin = it.cy;
        if (it.cy > yMax) yMax = it.cy;
      }
      const xSpread = xMax - xMin, ySpread = yMax - yMin;
      const cutV = xSpread > ySpread || (xSpread === ySpread && rect.w >= rect.h);
      group.sort((a, b) => cutV ? a.cx - b.cx : a.cy - b.cy);
      const mid = Math.floor(group.length / 2);
      const A = group.slice(0, mid), B = group.slice(mid);
      // Cut between the rightmost (bottommost) edge of A and the leftmost
      // (topmost) edge of B. Edge-based cut is idempotent under TILE_MARGIN:
      // after snap, A's max edge = cut - TILE_MARGIN, B's min edge = cut,
      // and round((cut - 1 + cut) / 2) = cut, so subsequent snaps don't drift.
      if (cutV) {
        let aMax = -Infinity, bMin = Infinity;
        for (const it of A) { const e = it.cx + it.cw / 2; if (e > aMax) aMax = e; }
        for (const it of B) { const e = it.cx - it.cw / 2; if (e < bMin) bMin = e; }
        const cut = Math.round((aMax + bMin) / 2);
        const c = Math.max(rect.x + 1, Math.min(cut, rect.x + rect.w - 1));
        partition({ x: rect.x, y: rect.y, w: c - rect.x, h: rect.h }, A);
        partition({ x: c, y: rect.y, w: rect.x + rect.w - c, h: rect.h }, B);
      } else {
        let aMax = -Infinity, bMin = Infinity;
        for (const it of A) { const e = it.cy + it.ch / 2; if (e > aMax) aMax = e; }
        for (const it of B) { const e = it.cy - it.ch / 2; if (e < bMin) bMin = e; }
        const cut = Math.round((aMax + bMin) / 2);
        const c = Math.max(rect.y + 1, Math.min(cut, rect.y + rect.h - 1));
        partition({ x: rect.x, y: rect.y, w: rect.w, h: c - rect.y }, A);
        partition({ x: rect.x, y: c, w: rect.w, h: rect.y + rect.h - c }, B);
      }
    }

    partition({ x: vx0, y: vy0, w: vx1 - vx0, h: vy1 - vy0 }, items);

    // If any leaf rect can't accommodate the window's min size, fall back to
    // tile. Always reserve the dock here: this is the "didn't fit" path and
    // a window may well end up minimized as part of recovery.
    for (const it of items) {
      if (it.rect.w - TILE_MARGIN < it.minW || it.rect.h - TILE_MARGIN < it.minH) {
        tile(wbs, { reserveDock: true });
        setLayout(LAYOUT.SNAP);  // restore -- tile() above overwrites it
        return;
      }
    }

    for (const it of items) {
      const r = it.rect;
      it.wb.resize(r.w - TILE_MARGIN, r.h - TILE_MARGIN).move(r.x, r.y);
      setShadow(it.wb, false);
    }
    zOrder(wbs);
  }

  // Window menu's Close All: explicit dismissal. Snapshot remains
  // restorable via the ribbon, but _closed=true blocks navigation reopen.
  function closeAll() {
    const liveSnap = snapshotLive();
    closeAllLiveGames();
    dismissWindows({ markClosed: true, liveSnap });
  }

  function focus() {
    for (const wb of openWindows()) {
      try { wb.focus(); } catch { /* */ }
    }
  }

  function hide() {
    detachResizeListeners();
    for (const wb of openWindows()) {
      try { wb.hide(); } catch { /* */ }
    }
  }

  function show() {
    for (const wb of openWindows()) {
      try { wb.show(); } catch { /* */ }
    }
    attachResizeListeners();
    requestAnimationFrame(reapplyLayout);
  }

  function isHidden() {
    const wbs = openWindows();
    return wbs.length > 0 && wbs.every(wb => wb.hidden);
  }

  function openSystemWindow(key, { flash = true } = {}) {
    if (windows[key]) {
      try {
        const wb = windows[key];
        if (wb.min) wb.restore();
        wb.focus();
        if (flash) flashWindow(wb);
      } catch {}
      return;
    }
    const spec = windowSpecs[key];
    const body = spec.makeBody();
    spec.setBody(body);
    windows[key] = makeBox(key, spec.title, body);
    spec.postCreate?.(windows[key]);
    spec.render();
    armSubscriptions();
    refresh();
    if (flash) requestAnimationFrame(() => { try { flashWindow(windows[key]); } catch {} });
    requestAnimationFrame(reapplyLayout);
  }

  function untidy() {
    setLayout(LAYOUT.NONE);
    for (const wb of openWindows()) setShadow(wb, true);
  }
  function reapplyLayout() {
    //console.log("[reapplyLayout] layout=", LAYOUT_NAME[activeLayout], "windows=", openWindows().length);
    if (activeLayout === LAYOUT.TIDY) tidy({ preserveMin: true });
    else if (activeLayout === LAYOUT.TILE) tile(null, { preserveMin: true, reserveDock: true });
    else if (activeLayout === LAYOUT.SNAP) snap();
  }
  function minimizeAll() {
    const wbs = openWindows().filter(wb => !wb.min);
    for (const wb of wbs) try { wb.minimize(); } catch { /* */ }
    return wbs;
  }
  function restoreWindows(wbs) {
    for (const wb of wbs) try { unminimize(wb); } catch { /* */ }
  }
  const workspace = { close, tile, tidy, untidy, snap, closeAll, minimizeAll, restoreWindows, focus, hide, show, isHidden, openSystemWindow, refresh, applyDetail, setOtherActive, tournamentId: tournament.id, get isTidy() { return activeLayout === LAYOUT.TIDY; } };
  activeWorkspace = workspace;
  return workspace;
}

export function getActiveWorkspace() {
  return activeWorkspace;
}

export function isTidyMode() {
  return activeLayout === LAYOUT.TIDY;
}

export function getActiveLayout() {
  return activeLayout;
}

export { LAYOUT };
