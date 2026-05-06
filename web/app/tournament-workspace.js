// Tournament workspace: WinBox windows for Standings, Schedule, Event log,
// plus on-demand Live Game windows (one per engine POV).
//
// State: one workspace per tab; opening a different tournament closes the
// prior one. User-closed windows do not auto-reopen on events. Layout
// (position/size) persisted per-window in localStorage.
//
// Data flow:
//   GET /api/tournaments/{id} on open → seed standings + schedule.
//   WS `tournament_status`             → refresh metadata + standings.
//   WS `tournament_update`             → event log; refresh on game-finished.
//   Periodic GET while running         → reconcile standings.

import { closeAllLiveGames, closeStaleLiveGames, getLiveWindows, isLiveWindowOpen, openLiveGameWindow } from "./tournament-live-game.js";
import { EVT, EVT_PREFIX, KIND, STATUS } from "./tournament-events.js";
import { toast } from "./dialogs.js";
import { escapeHtml, flashWindow } from "./wb-utils.js";

const STORAGE_KEY = "sturddle:tournament-workspace-layout";
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


function loadLayout() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_LAYOUT };
    const parsed = JSON.parse(raw);
    return { ...DEFAULT_LAYOUT, ...parsed };
  } catch {
    return { ...DEFAULT_LAYOUT };
  }
}

function saveLayout(layout) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(layout));
  } catch {
    // Storage may be disabled (private mode quotas); best-effort.
  }
}


let activeWorkspace = null;


export function openTournamentWorkspace({ api, events, log, token, tournament, top = 0, left = 0 }) {
  // Single-active model. Re-clicking the workspace icon for the
  // already-open tournament is a no-op (just focus its windows) so
  // attached engine windows survive — closing here would tear them
  // down via tearDown's closeAllLiveGames().
  if (activeWorkspace) {
    if (activeWorkspace.tournamentId === tournament.id) return activeWorkspace;
    activeWorkspace.close();
    activeWorkspace = null;
  }

  const layout = loadLayout();
  let detail = null;
  const eventLog = [];
  // Server-stamped sequence numbers we've already added to eventLog.
  // Lets us run the WS subscription in parallel with the REST backfill
  // without showing duplicates around workspace open.
  const seenSeqs = new Set();
  let pollTimer = null;
  let unsubscribe = null;
  // proxy_id -> { engineName }
  const activeProxies = new Map();
  // proxy_id -> { pairId, proxyA, engineA, sideA, proxyB, engineB, sideB }
  // Both proxies in a pair map to the same info object.
  const livePairings = new Map();

  // ---- Window construction ----------------------------------------------

  function makeStandingsBody() {
    const el = document.createElement("div");
    el.className = "wb-standings";
    el.innerHTML = `<div class="wb-empty">Loading…</div>`;
    return el;
  }
  function makeScheduleBody() {
    const el = document.createElement("div");
    el.className = "wb-schedule";
    el.innerHTML = `<div class="wb-empty">Loading…</div>`;
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
    el.innerHTML = `<div class="wb-empty">Loading…</div>`;
    return el;
  }

  // Renderers read these via the closure; reassigned when a window is
  // re-opened after the user closed it (so renderers target the new body).
  let standingsBody = makeStandingsBody();
  let scheduleBody = makeScheduleBody();
  let logBody = makeLogBody();
  let enginesBody = makeEnginesBody();

  const MIN_SIZES = {
    standings: { minwidth: 320, minheight: 200 },
    schedule:  { minwidth: 320, minheight: 200 },
    engines:   { minwidth: 280, minheight: 200 },
    log:       { minwidth: 280, minheight: 150 },
  };

  function makeBox(key, title, body) {
    const cfg = layout[key];
    const wb = new WinBox({
      title,
      x: cfg.x,
      y: cfg.y,
      width: cfg.width,
      height: cfg.height,
      top,
      left,
      mount: body,
      class: "sturddle-wb no-full",
      ...MIN_SIZES[key],
    });
    // Wire callbacks after construction so they can refer to `wb` itself
    // (avoids a TDZ "cannot access wb before initialization" error from
    // wiring them inside the constructor options object).
    wb.onclose = () => {
      layout[key] = wb_currentLayout(wb);
      saveLayout(layout);
      windows[key] = null;
      if (Object.values(windows).every((w) => w === null)) {
        tearDown();
      }
      return false; // allow close
    };
    wb.onresize = () => persistLayout(key, wb);
    wb.onmove = () => persistLayout(key, wb);
    if (top > 0 && wb.y < top) wb.move(wb.x, top);
    if (left > 0 && wb.x < left) wb.move(left, wb.y);
    return wb;
  }

  function wb_currentLayout(wb) {
    // WinBox exposes width/height/x/y as numeric pixel values on the
    // instance after construction. Persist as plain integers; on reload
    // we'll convert back to "Npx" strings.
    return {
      x: `${Math.round(wb.x)}px`,
      y: `${Math.round(wb.y)}px`,
      width: `${Math.round(wb.width)}px`,
      height: `${Math.round(wb.height)}px`,
    };
  }

  function persistLayout(key, wb) {
    layout[key] = wb_currentLayout(wb);
    saveLayout(layout);
  }

  const windowSpecs = {
    standings: {
      title: `Standings | ${tournament.name}`,
      makeBody: makeStandingsBody,
      setBody: (b) => { standingsBody = b; },
      render: () => renderStandings(),
    },
    schedule: {
      title: `Live Games | ${tournament.name}`,
      makeBody: makeScheduleBody,
      setBody: (b) => { scheduleBody = b; },
      render: () => renderSchedule(),
    },
    engines: {
      title: `Engine Instances | ${tournament.name}`,
      makeBody: makeEnginesBody,
      setBody: (b) => { enginesBody = b; },
      render: () => renderEngines(),
    },
    log: {
      title: `Event log | ${tournament.name}`,
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
                  parts.push(`${e.payload?.engine_a}(${(e.payload?.proxy_a||"").slice(0,8)}) vs ${e.payload?.engine_b}(${(e.payload?.proxy_b||"").slice(0,8)})`);
                else if (inner === KIND.GAME_FINISHED) {
                  const result = e.payload?.result || "*";
                  const termination = e.payload?.termination;
                  parts.push(`${e.payload?.engine_a} vs ${e.payload?.engine_b}`,
                             termination ? `${result} ${termination}` : result);
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

  const windows = {
    standings: makeBox("standings", windowSpecs.standings.title, standingsBody),
    schedule:  makeBox("schedule",  windowSpecs.schedule.title,  scheduleBody),
    engines:   null,
    log:       null,
  };

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
          <td>${e.elo == null ? "—" : (e.elo >= 0 ? "+" : "") + e.elo.toFixed(1) + (e.elo_margin_95 == null ? "" : ` ± ${e.elo_margin_95.toFixed(1)}`)}</td>
        </tr>`)
      .join("");
    const sprtRow = sprt
      ? `<div class="wb-sprt">SPRT [${sprt.elo0}, ${sprt.elo1}] · LLR=${sprt.llr.toFixed(2)} ` +
        `[${sprt.lower_bound.toFixed(2)}, ${sprt.upper_bound.toFixed(2)}] · ${sprt.status}</div>`
      : "";
    standingsBody.innerHTML = `
      ${sprtRow}
      <table class="wb-table">
        <thead>
          <tr><th>Engine</th><th>G</th><th>W</th><th>L</th><th>D</th><th>%</th><th>Elo</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  async function attachWatch(btn, attachKey, sourceWindowKey, openOpts) {
    let boardStyle = null;
    try { const s = await api("GET", "/settings"); boardStyle = s.board_style || null; } catch {}
    const src = windows[sourceWindowKey];
    const avoidRect = src ? { x: src.x, y: src.y, w: src.width, h: src.height } : null;
    openLiveGameWindow({ ...openOpts, token, top, left, boardStyle, avoidRect });
    btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(attachKey));
  }

  function renderSchedule() {
    if (livePairings.size === 0) {
      scheduleBody.innerHTML = `<div class="wb-empty">No games yet.</div>`;
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
        <span class="wb-sched-icon">♟</span>
        <span class="wb-sched-game">${escapeHtml(wLabel)} – ${escapeHtml(bLabel)}</span>
      `;
      const btn = document.createElement("button");
      btn.className = "wb-sched-attach-btn";
      btn.textContent = "watch";
      btn.title = info.pairId || key;
      btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(info.pairId || key));
      btn.addEventListener("click", () => attachWatch(btn, info.pairId || key, "schedule", {
        proxyId: info.proxyA,
        gameId: info.pairId || null,
        label: `${wLabel} vs ${bLabel} | ${tournament.name}`,
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
        <span class="wb-sched-icon">▶</span>
        <span class="wb-sched-game">${escapeHtml(engineLabel)}</span>
      `;
      const btn = document.createElement("button");
      btn.className = "wb-sched-attach-btn";
      btn.textContent = "watch";
      btn.title = pid;
      btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(pid));
      btn.addEventListener("click", () => attachWatch(btn, pid, "engines", {
        proxyId: pid,
        label: `${engineLabel} | ${tournament.name}`,
        engineName: engineLabel,
      }));
      li.appendChild(btn);
      list.appendChild(li);
    }
    if (atBottom && scroller) scroller.scrollTop = scroller.scrollHeight;
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
        const result = e.payload?.result || "*";
        const termination = e.payload?.termination;
        const tail = termination ? `${result} ${termination}` : result;
        parts.push(inner, `${a} vs ${b}`, tail);
      } else if (inner === KIND.PROXY_PAIRED) {
        const a = e.payload?.engine_a || "?";
        const b = e.payload?.engine_b || "?";
        const pa = (e.payload?.proxy_a || "").slice(0, 8);
        const pb = (e.payload?.proxy_b || "").slice(0, 8);
        parts.push(inner, `${a}(${pa}) vs ${b}(${pb})`);
      } else if (inner === KIND.PROXY_UNPAIRED) {
        const pa = (e.payload?.proxy_id || "").slice(0, 8);
        const pb = (e.payload?.peer_id  || "").slice(0, 8);
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
    // Seed active proxies from server snapshot — authoritative; replace
    // wholesale so we drop any rows for proxies the server no longer
    // tracks (e.g. ended sessions whose proxy_ended event we missed).
    const seeded = detail.proxies_active || [];
    activeProxies.clear();
    for (const p of seeded) {
      if (p.proxy_id) {
        activeProxies.set(p.proxy_id, { engineName: p.engine_name || null });
      }
    }
    // Seed confirmed pairings — same authoritative replace so late-opening
    // workspaces don't depend on having caught every proxy_paired WS event.
    livePairings.clear();
    for (const p of (detail.pairings_active || [])) {
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
    // Only events for *our* tournament — the orchestrator stamps
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
      // Authoritative game-end signal — drives livePairings cleanup +
      // Schedule re-render. PROXY_UNPAIRED still arrives (debug signal,
      // see _dissolve_pair) but is filtered from the visible event log.
      livePairings.delete(evt.payload.proxy_a);
      livePairings.delete(evt.payload.proxy_b);
    } else if (
      evt.kind === EVT.STATUS ||
      inner === KIND.DONE || inner === KIND.STOPPED
    ) {
      activeProxies.clear();
    }

    if (added) renderEventLog();
    if (inner === KIND.PROXY_STARTED || inner === KIND.PROXY_ENDED ||
        inner === KIND.PROXY_PAIRED || inner === KIND.PROXY_UNPAIRED ||
        inner === KIND.GAME_FINISHED || evt.kind === EVT.STATUS ||
        inner === KIND.DONE || inner === KIND.STOPPED)
      renderSchedule();
    if (inner === KIND.PROXY_STARTED || inner === KIND.PROXY_ENDED ||
        evt.kind === EVT.STATUS || inner === KIND.DONE || inner === KIND.STOPPED)
      renderEngines();

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

    // Tournament terminal state: drop stale live windows (proxy-id
    // attaches always go stale; game-id windows that were force-
    // dissolved without a real result also stale). Resolved game-id
    // windows keep their final banner. Standard windows stay open
    // so the user can review final state.
    if (
      evt.kind === EVT.STATUS &&
      [STATUS.STOPPED, STATUS.DONE, STATUS.FAILED].includes(evt.payload?.status)
    ) {
      closeStaleLiveGames();
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
      if (added) renderEventLog();
    } catch (e) {
      log?.(`event backfill failed: ${e.message}`);
    }
  }
  async function initWorkspace() {
    await Promise.all([refresh(), backfillEvents()]);
    if (eventLog.length > 0 || detail?.status === STATUS.RUNNING) {
      openSystemWindow("log");
    }
  }
  initWorkspace();

  // Periodic poll: events should drive most updates, but a poll catches
  // server-restart catch-up windows and PGN-only changes (e.g. the
  // server's pgn_stats picks up games we missed via WS).
  pollTimer = window.setInterval(() => {
    if (detail?.status === STATUS.RUNNING) refresh();
  }, POLL_INTERVAL_MS);
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
    window.removeEventListener("sturddle:livegame-closed", refreshWatchButtons);
    if (liveWatcherAttached) {
      window.removeEventListener("sturddle:livegame-closed", onLiveGameClosed);
      liveWatcherAttached = false;
    }
    if (activeWorkspace === workspace) activeWorkspace = null;
    // Notify the perspective so the Window menu re-syncs even when
    // the user closed the last standard window via its X button
    // (rather than the Close-all menu item).
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
    // While watch windows are still open, keep this workspace "active"
    // so the Window menu's Tile/Cascade/Close All can still operate on
    // them. Defer finalization until the last live window closes.
    if (getLiveWindows().length > 0) {
      if (!liveWatcherAttached) {
        window.addEventListener("sturddle:livegame-closed", onLiveGameClosed);
        liveWatcherAttached = true;
      }
      return;
    }
    finalize();
  }

  function close() {
    // Tournament-switch path: close standard windows and stale live
    // windows (proxy-id attaches and unresolved game-id windows).
    // Resolved game-id windows persist with their final banner so
    // the user can still review them. tearDown() defers finalize()
    // until the last surviving live window is closed manually. The
    // menu's "Close all" (closeAll) closes everything unconditionally.
    for (const k of Object.keys(windows)) {
      if (windows[k]) {
        windows[k].close(true); // skip the onclose callback's tearDown loop
        windows[k] = null;
      }
    }
    closeStaleLiveGames();
    tearDown();
  }

  function openWindows() {
    return [...Object.values(windows).filter(Boolean), ...getLiveWindows()];
  }

  function unminimize(wb) {
    // resize/move on a minimized or maximized WinBox leaves it stuck in
    // that state — restore first so the new geometry actually takes.
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
      // (WinBox doesn't expose its config min* on the instance — windows
      // that need clamping stash svMinWidth / svMinHeight at creation.)
      const ww = Math.max(w, wb.svMinWidth || 0);
      const hh = Math.max(h, wb.svMinHeight || 0);
      wb.resize(ww, hh).move(left + col * w, top + row * h);
    });
  }

  function cascade() {
    const wbs = openWindows();
    const offset = 30;
    wbs.forEach((wb, i) => {
      unminimize(wb);
      wb.move(left + i * offset, top + i * offset);
    });
  }

  function closeAll() {
    closeAllLiveGames();
    close();
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

  const workspace = { close, tile, cascade, closeAll, focus, hide, show, isHidden, openSystemWindow, tournamentId: tournament.id };
  activeWorkspace = workspace;
  return workspace;
}

export function getActiveWorkspace() {
  return activeWorkspace;
}
