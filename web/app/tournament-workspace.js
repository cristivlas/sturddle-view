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
  closeAllLiveGames, closeStaleLiveGames, getLiveWindows,
  isLiveWindowOpen, openLiveGameWindow,
  LIVE_MIN_WIDTH, LIVE_MIN_HEIGHT, DEBUG_WATCH,
} from "./tournament-live-game.js";
import { EVT, EVT_PREFIX, KIND, STATUS } from "./tournament-events.js";
import { toast } from "./dialogs.js";
import { escapeHtml, flashWindow } from "./wb-utils.js";
import { createSlotGrid } from "./workspace-slot-grid.js";

const STORAGE_KEY_PREFIX = "sturddle:workspace:";
const POLL_INTERVAL_MS = 5000;
const EVENT_LOG_LIMIT = 500;

// Default layout, in viewport-percent units. WinBox accepts strings like
// "40%". Cast to strings at use time.
const DEFAULT_LAYOUT = {
  standings: { x: "1%",  y: "1%",  width: "40%", height: "50%" },
  schedule:  { x: "1%",  y: "52%", width: "40%", height: "47%" },
  engines:   { x: "42%", y: "1%",  width: "30%", height: "50%" },
  log:       { x: "42%", y: "70%", width: "57%", height: "29%" },
};


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


let activeWorkspace = null;


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
  const lastGeometry = {};
  for (const key of Object.keys(DEFAULT_LAYOUT)) {
    const s = savedState?.[key];
    lastGeometry[key] = s
      ? { x: s.x, y: s.y, width: s.width, height: s.height }
      : { ...DEFAULT_LAYOUT[key] };
  }
  let detail = null;
  const eventLog = [];
  // Server-stamped sequence numbers we've already added to eventLog.
  // Lets us run the WS subscription in parallel with the REST backfill
  // without showing duplicates around workspace open.
  const seenSeqs = new Set();
  let pollTimer = null;
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
  const slotGrid = createSlotGrid({
    top, left,
    getCellWidth: () => Math.max(LIVE_MIN_WIDTH, Math.round(window.innerWidth * 0.20)),
    cellHeight: LIVE_MIN_HEIGHT,
    getWindows: () => getLiveWindows(),
  });
  // Horizontal cascade for overflow-restore (no slot available):
  // successive restores step right so they don't stack.
  const OVERFLOW_X_OFFSET = 24;
  let overflowRestoreCount = 0;

  // ---- Window construction ----------------------------------------------

  function makeStandingsBody() {
    const el = document.createElement("div");
    el.className = "wb-standings";
    el.innerHTML = `<div class="wb-empty">Loading...</div>`;
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

  const MIN_SIZES = {
    standings: { minwidth: 320, minheight: 150 },
    schedule:  { minwidth: 320, minheight: 150 },
    engines:   { minwidth: 280, minheight: 150 },
    log:       { minwidth: 280, minheight: 150 },
  };

  // Per-window CSS class hooks (added to the WinBox outer container).
  // schedule = "Live Games" panel; gets a stable scrollbar gutter to
  // avoid width pulsation when rows come and go.
  const EXTRA_CLASS = {
    schedule: "sturddle-wb-live-games",
  };

  function makeBox(key, title, body, { min = false, max = false } = {}) {
    const cfg = lastGeometry[key];
    const extra = EXTRA_CLASS[key] ? ` ${EXTRA_CLASS[key]}` : "";
    const wb = new WinBox({
      title, mount: body, top, left, min, max,
      x: cfg.x, y: cfg.y, width: cfg.width, height: cfg.height,
      class: `sturddle-wb no-full${extra}`,
      ...MIN_SIZES[key],
    });
    // Wire onclose after construction (TDZ on `wb` otherwise). No persist
    // here -- state is captured at workspace.close()/closeAll()/finalize().
    wb.onclose = () => {
      lastGeometry[key] = wbGeometry(wb);
      windows[key] = null;
      if (Object.values(windows).every((w) => w === null)) tearDown();
      return false;
    };
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

  function snapshot() {
    const state = {};
    for (const key of Object.keys(windows)) {
      const wb = windows[key];
      state[key] = wb
        ? { open: true, ...wbGeometry(wb), min: !!wb.min, max: !!wb.max, z: wb.index ?? 0 }
        : { open: false, ...lastGeometry[key], min: false, max: false, z: 0 };
    }
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
          index: 3,
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

  // ---- Rendering --------------------------------------------------------

  function renderStandings() {
    const standings = detail?.standings;
    if (!standings || standings.engines.length === 0) {
      standingsBody.innerHTML = `<div class="wb-empty">No games played yet.</div>`;
      return;
    }
    const sprt = detail.sprt;
    const rows = standings.engines
      .map((e) => `
        <tr>
          <td class="wb-eng-name">${escapeHtml(e.name)}</td>
          <td>${e.games}</td>
          <td>${e.wins}</td>
          <td>${e.losses}</td>
          <td>${e.draws}</td>
          <td>${(e.score_pct * 100).toFixed(1)}%</td>
          <td>${e.elo == null ? "--" : (e.elo >= 0 ? "+" : "") + e.elo.toFixed(1) + (e.elo_margin_95 == null ? "" : ` +/- ${e.elo_margin_95.toFixed(1)}`)}</td>
        </tr>`)
      .join("");
    const sprtRow = sprt
      ? `<div class="wb-sprt">SPRT [${sprt.elo0}, ${sprt.elo1}] * LLR=${sprt.llr.toFixed(2)} ` +
        `[${sprt.lower_bound.toFixed(2)}, ${sprt.upper_bound.toFixed(2)}] * ${sprt.status}</div>`
      : "";
    const partialPairs = detail.partial_pairs ?? 0;
    // Hide during RUNNING -- a fresh game-1 always sits alone in the
    // PGN until game-2 of the pair finishes; that's normal, not data loss.
    const showPartial = partialPairs > 0 && detail.status !== STATUS.RUNNING;
    const partialRow = showPartial
      ? `<div class="wb-partial-pairs">${partialPairs} incomplete pair${partialPairs === 1 ? "" : "s"} ` +
        `(one game missing, likely lost when paused)</div>`
      : "";
    standingsBody.innerHTML = `
      ${sprtRow}
      ${partialRow}
      <table class="wb-table">
        <thead>
          <tr><th>Engine</th><th>G</th><th>W</th><th>L</th><th>D</th><th>%</th><th>Elo</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  function attachWatch(btn, attachKey, sourceWindowKey, openOpts) {
    if (DEBUG_WATCH) console.log("[WATCH] click", { attachKey, sourceWindowKey, openOpts });
    // Claim a slot BEFORE creating the window so the new window's own
    // default position doesn't shadow the slot it would occupy.
    const rawClaim = isLiveWindowOpen(attachKey) ? null : slotGrid.claim();
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
        top, left, boardStyle: boardStyleCached,
        initialRect: claim ? { x: claim.x, y: claim.y, w: claim.w, h: claim.h } : null,
        // Only overflow windows get a restore callback -- slotted windows
        // already have a position and restore to it naturally.
        onAfterRestore: !claim ? (wb) => {
          const c = slotGrid.claim();
          if (c) {
            wb.resize(c.w, c.h).move(c.x, c.y);
            overflowRestoreCount = 0;
            return;
          }
          const x = Math.min(
            left + overflowRestoreCount * OVERFLOW_X_OFFSET,
            Math.max(left, window.innerWidth - wb.width),
          );
          wb.move(x, top);
          overflowRestoreCount++;
        } : null,
      });
    } catch (e) {
      console.error("[WATCH] openLiveGameWindow threw", e, { attachKey, openOpts });
      return;
    }
    // No slot fit -- minimize so the grid stays clean. WS already
    // connected; live state stays current behind the minimize bar.
    if (result?.wb && !result.alreadyOpen && !claim) {
      try { result.wb.minimize(); } catch { /* */ }
    }
    const isLive = isLiveWindowOpen(attachKey);
    if (DEBUG_WATCH) console.log("[WATCH] post-open", { attachKey, isLive, slotted: !!claim });
    btn.classList.toggle("wb-sched-attach-btn--live", isLive);
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
    const atBottom = !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
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

    if (atBottom && scroller) scroller.scrollTop = scroller.scrollHeight;
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
    const atBottom = !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
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
    if (atBottom && scroller) scroller.scrollTop = scroller.scrollHeight;
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
        const tail = (err.stderr_tail || []).slice(-10).join("\n") || `exit code ${err.rc}`;
        banner.innerHTML = `<div class="wb-error-title">Tournament failed (rc=${err.rc})</div><pre>${escapeHtml(tail)}</pre>`;
        banner.hidden = false;
      } else {
        banner.hidden = true;
        banner.innerHTML = "";
      }
    }
    const list = logBody.querySelector(".wb-eventlog-list");
    if (!list) return;
    const scroller = logBody.parentElement;
    const atBottom = !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
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
    if (atBottom && scroller) scroller.scrollTop = scroller.scrollHeight;
  }

  // ---- Data refresh -----------------------------------------------------

  async function refresh() {
    try {
      detail = await api("GET", `/api/tournaments/${tournament.id}`);
    } catch (e) {
      log?.(`workspace refresh failed: ${e.message}`);
      return;
    }
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
    if (seq != null) {
      if (seenSeqs.has(seq)) return false;
      seenSeqs.add(seq);
    }
    // `game_reconciled` upgrades an existing game_finished row in place
    // (see pushEvent) -- don't surface a second row for the same game.
    // Still mark seq seen above so backfill replays are deduped.
    if (evt.payload?.kind === KIND.GAME_RECONCILED) return false;
    const tsRaw = evt.payload?._ts;
    const ts = (tsRaw ? new Date(tsRaw) : new Date())
      .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    eventLog.push({ ts, kind: evt.kind, payload: evt.payload, _seq: seq });
    // Keep ordered by seq so backfill items slot in before any live
    // events that arrived during the REST round-trip.
    eventLog.sort((a, b) => (a._seq ?? 0) - (b._seq ?? 0));
    while (eventLog.length > EVENT_LOG_LIMIT) eventLog.shift();
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

    // Tournament terminal state: close all live windows (result is
    // always UNKNOWN, nothing to review post-game).
    if (
      evt.kind === EVT.STATUS &&
      [STATUS.STOPPED, STATUS.DONE, STATUS.FAILED].includes(evt.payload?.status)
    ) {
      closeStaleLiveGames();
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
    if (pollTimer == null) {
      pollTimer = window.setInterval(() => {
        if (detail?.status === STATUS.RUNNING) refresh();
      }, POLL_INTERVAL_MS);
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
      if (detail?.status === STATUS.RUNNING) openSystemWindow("schedule");
      if (eventLog.length > 0 || detail?.status === STATUS.RUNNING) openSystemWindow("log");
    }
  }
  initWorkspace();

  // Periodic poll: events should drive most updates, but a poll catches
  // server-restart catch-up windows and PGN-only changes (e.g. the
  // server's pgn_stats picks up games we missed via WS).
  pollTimer = window.setInterval(() => {
    if (detail?.status === STATUS.RUNNING) refresh();
  }, POLL_INTERVAL_MS);
  function onReconnect(e) {
    if (!e.detail?.connected) {
      seenSeqs.clear();
      eventLog.length = 0;
      return;
    }
    refresh();
    backfillEvents();
  }
  window.addEventListener("sturddle:connection", onReconnect);
  window.addEventListener("sturddle:livegame-closed", refreshWatchButtons);

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
    window.removeEventListener("sturddle:connection", onReconnect);
    window.removeEventListener("sturddle:livegame-closed", refreshWatchButtons);
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
    if (pollTimer != null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
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
  function dismissWindows({ markClosed }) {
    const state = snapshot();
    if (markClosed) state._closed = true;
    saveState(tournament.id, state);
    explicitlyClosed = true;
    for (const k of Object.keys(windows)) {
      if (windows[k]) {
        windows[k].close(true);
        windows[k] = null;
      }
    }
    closeStaleLiveGames();
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

  function tile() {
    const wbs = openWindows();
    if (!wbs.length) return;
    const availW = window.innerWidth - left;
    const availH = window.innerHeight - top;
    const cols = Math.ceil(Math.sqrt(wbs.length));
    const rows = Math.ceil(wbs.length / cols);
    const w = Math.floor(availW / cols);
    const h = Math.floor(availH / rows);
    wbs.forEach((wb, i) => {
      unminimize(wb);
      const col = i % cols;
      const row = Math.floor(i / cols);
      // Clamp to per-window minimums so live-game layout stays usable.
      // (WinBox doesn't expose its config min* on the instance -- windows
      // that need clamping stash svMinWidth / svMinHeight at creation.)
      const ww = Math.max(w, wb.svMinWidth || 0);
      const hh = Math.max(h, wb.svMinHeight || 0);
      wb.resize(ww, hh).move(left + col * w, top + row * h);
    });
  }

  // 2x2 in the bottom half of the viewport. Auto-opens any of the
  // four target windows that aren't open yet. Reserves a footer strip
  // at the bottom so minimized WinBoxes have a place to dock.
  const MINIMIZE_FOOTER_H = 40;
  function tidy() {
    const keys = ["engines", "standings", "schedule", "log"];
    for (const k of keys) {
      if (!windows[k]) openSystemWindow(k);
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
      watchers.forEach((wb, i) => {
        if (i < cap) {
          unminimize(wb);
          const r = slotGrid.rectAt(i);
          wb.resize(r.w, r.h).move(r.x, r.y);
        } else {
          try { wb.minimize(); } catch { /* */ }
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
    const rightW = availW - leftW;
    // Clamp each row to the tallest minheight in that row so both
    // windows in a row resize to the same height (otherwise WinBox
    // silently floors to per-window minheight, misaligning bottoms).
    const desiredRowH = Math.floor(availH * 0.25);
    const topRowH = Math.max(desiredRowH, MIN_SIZES.engines.minheight, MIN_SIZES.standings.minheight);
    const botRowH = Math.max(desiredRowH, MIN_SIZES.schedule.minheight, MIN_SIZES.log.minheight);
    // Anchor bottom edge to top + availH (which already excludes the
    // minimize footer). If clamped rows exceed availH the layout
    // extends upward, but never below the reserved footer.
    const regionTop = top + availH - (topRowH + botRowH);
    const placements = [
      ["engines",   left,         regionTop,            leftW,  topRowH],
      ["standings", left + leftW, regionTop,            rightW, topRowH],
      ["schedule",  left,         regionTop + topRowH, leftW,  botRowH],
      ["log",       left + leftW, regionTop + topRowH, rightW, botRowH],
    ];
    for (const [k, x, y, w, h] of placements) {
      const wb = windows[k];
      if (!wb) continue;
      unminimize(wb);
      wb.resize(w, h).move(x, y);
    }

  }

  // Window menu's Close All: explicit dismissal. Snapshot remains
  // restorable via the ribbon, but _closed=true blocks navigation reopen.
  function closeAll() {
    closeAllLiveGames();
    dismissWindows({ markClosed: true });
  }

  function focus() {
    for (const wb of openWindows()) {
      try { wb.focus(); } catch { /* */ }
    }
  }

  function hide() {
    for (const wb of openWindows()) {
      try { wb.hide(); } catch { /* */ }
    }
  }

  function show() {
    for (const wb of openWindows()) {
      try { wb.show(); } catch { /* */ }
    }
  }

  function isHidden() {
    const wbs = openWindows();
    return wbs.length > 0 && wbs.every(wb => wb.hidden);
  }

  function openSystemWindow(key) {
    if (windows[key]) {
      try {
        const wb = windows[key];
        if (wb.min) wb.restore();
        wb.focus();
        flashWindow(wb);
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
    requestAnimationFrame(() => { try { flashWindow(windows[key]); } catch {} });
  }

  const workspace = { close, tile, tidy, closeAll, focus, hide, show, isHidden, openSystemWindow, tournamentId: tournament.id };
  activeWorkspace = workspace;
  return workspace;
}

export function getActiveWorkspace() {
  return activeWorkspace;
}
