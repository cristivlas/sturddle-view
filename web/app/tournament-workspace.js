// Tournament workspace: three WinBox windows (Standings, Schedule, Event log).
// Slice 9c adds the on-demand Live Game window: clicking an in-progress
// row in the Schedule subscribes to one engine's proxy stream and
// renders the position from that engine's POV.

import { closeAllLiveGames, getLiveWindows, openLiveGameWindow } from "./tournament-live-game.js";
//
// State model:
//   - One workspace open at a time per tab. Opening a workspace for a
//     different tournament closes the previous one.
//   - The user can close any window via its X; it won't auto-reopen on
//     subsequent events. To bring it back, re-click "Open workspace".
//   - Layout (positions/sizes) is persisted per-window in localStorage as
//     the user's preferred layout. First open uses the spec's defaults.
//
// Data flow:
//   - GET /api/tournaments/{id} on open → seed standings + schedule.
//   - WS `tournament_status`              → refresh metadata + standings.
//   - WS `tournament_update`              → push to event log; refresh on
//                                           game-finished kinds.
//   - Periodic GET while running           → catch standings updates we
//                                           inferred from events but didn't
//                                           recompute in the browser.

const STORAGE_KEY = "sturddle:tournament-workspace-layout";
const POLL_INTERVAL_MS = 5000;
const EVENT_LOG_LIMIT = 500;

// Default layout, in viewport-percent units. WinBox accepts strings like
// "40%". Cast to strings at use time.
const DEFAULT_LAYOUT = {
  standings: { x: "1%",  y: "1%",  width: "40%", height: "50%" },
  schedule:  { x: "1%",  y: "52%", width: "40%", height: "47%" },
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
    if (activeWorkspace.tournamentId === tournament.id) {
      activeWorkspace.ensureWindows();
      activeWorkspace.focus();
      return activeWorkspace;
    }
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
    el.innerHTML = `<ul class="wb-eventlog-list"></ul>`;
    return el;
  }

  // Renderers read these via the closure; reassigned when a window is
  // re-opened after the user closed it (so renderers target the new body).
  let standingsBody = makeStandingsBody();
  let scheduleBody = makeScheduleBody();
  let logBody = makeLogBody();

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
      title: `${tournament.name} — Standings`,
      makeBody: makeStandingsBody,
      setBody: (b) => { standingsBody = b; },
      render: () => renderStandings(),
    },
    schedule: {
      title: `${tournament.name} — Schedule`,
      makeBody: makeScheduleBody,
      setBody: (b) => { scheduleBody = b; },
      render: () => renderSchedule(),
    },
    log: {
      title: `${tournament.name} — Event log`,
      makeBody: makeLogBody,
      setBody: (b) => { logBody = b; },
      render: () => renderEventLog(),
    },
  };

  const windows = {
    standings: makeBox("standings", windowSpecs.standings.title, standingsBody),
    schedule:  makeBox("schedule",  windowSpecs.schedule.title,  scheduleBody),
    log:       makeBox("log",       windowSpecs.log.title,       logBody),
  };

  function ensureWindows() {
    for (const key of Object.keys(windowSpecs)) {
      if (windows[key]) continue;
      const spec = windowSpecs[key];
      const body = spec.makeBody();
      spec.setBody(body);
      windows[key] = makeBox(key, spec.title, body);
      spec.render();
    }
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
          <td class="wb-eng-name">${escape(e.name)}</td>
          <td>${e.games}</td>
          <td>${e.wins}</td>
          <td>${e.losses}</td>
          <td>${e.draws}</td>
          <td>${(e.score_pct * 100).toFixed(1)}%</td>
          <td>${e.elo == null ? "—" : e.elo.toFixed(1)}</td>
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

  function renderSchedule() {
    // Completed games: PGN-derived (authoritative once fastchess flushes
    // each finished game). In-progress: one row per active proxy
    // (engine process), labeled with its engine name. Click "watch" to
    // open a live window subscribed to that engine's stream.
    const finished = (detail && detail.games) || [];
    const inProgress = [...activeProxies.entries()];
    if (finished.length === 0 && inProgress.length === 0) {
      scheduleBody.innerHTML = `<div class="wb-empty">No games yet.</div>`;
      return;
    }
    scheduleBody.innerHTML = `<ul class="wb-sched-list"></ul>`;
    const list = scheduleBody.querySelector(".wb-sched-list");

    for (const g of finished) {
      const li = document.createElement("li");
      li.innerHTML = `
        <span class="wb-sched-icon">✓</span>
        ${escape(g.white)} – ${escape(g.black)}
        <span class="wb-sched-result">${escape(g.result)}</span>
      `;
      list.appendChild(li);
    }
    for (const [pid, p] of inProgress) {
      const li = document.createElement("li");
      li.className = "wb-sched-live";
      const engineLabel = p.engineName || pid;
      li.innerHTML = `
        <span class="wb-sched-icon">▶</span>
        <span class="wb-sched-game">${escape(engineLabel)}</span>
      `;
      const btn = document.createElement("button");
      btn.className = "wb-sched-attach-btn";
      btn.textContent = "watch";
      btn.title = pid;
      btn.addEventListener("click", async () => {
        let boardStyle = null;
        try {
          const s = await api("GET", "/settings");
          boardStyle = s.board_style || null;
        } catch {
          // ignore — fall back to default style
        }
        openLiveGameWindow({
          proxyId: pid,
          label: `${tournament.name} — ${engineLabel}`,
          token,
          top,
          left,
          boardStyle,
        });
      });
      li.appendChild(btn);
      list.appendChild(li);
    }
  }

  function renderEventLog() {
    const list = logBody.querySelector(".wb-eventlog-list");
    if (!list) return;
    list.innerHTML = eventLog.map((e) => {
      const ts = e.ts || "";
      const k = e.payload?.kind || e.kind || "event";
      // runner_log carries fastchess's own stdout/stderr output;
      // surface the actual line, not just the kind.
      if (k === "runner_log" && e.payload?.line) {
        const stream = e.payload.stream === "err" ? " err" : "";
        return `<li><span class="wb-log-ts">${ts}</span>` +
          `<span class="wb-log-runner${stream}">${escape(e.payload.line)}</span></li>`;
      }
      return `<li><span class="wb-log-ts">${ts}</span> <span class="wb-log-kind">${escape(k)}</span></li>`;
    }).join("");
    list.scrollTop = list.scrollHeight;
  }

  function escape(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
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
    renderStandings();
    renderSchedule();
  }

  function addLogEntry(evt) {
    const seq = evt.payload?._seq;
    if (seq != null) {
      if (seenSeqs.has(seq)) return false;
      seenSeqs.add(seq);
    }
    const tsRaw = evt.payload?._ts;
    const ts = tsRaw
      ? tsRaw.substring(11, 19)
      : new Date().toISOString().substring(11, 19);
    eventLog.push({ ts, kind: evt.kind, payload: evt.payload, _seq: seq });
    // Keep ordered by seq so backfill items slot in before any live
    // events that arrived during the REST round-trip.
    eventLog.sort((a, b) => (a._seq ?? 0) - (b._seq ?? 0));
    while (eventLog.length > EVENT_LOG_LIMIT) eventLog.shift();
    return true;
  }

  function pushEvent(evt) {
    if (!evt) return;
    if (!evt.kind?.startsWith("tournament_")) return;
    // Only events for *our* tournament — the orchestrator stamps
    // tournament_id into payloads on the server side.
    const tid = evt.payload?.tournament_id;
    if (tid && tid !== tournament.id) return;

    const added = addLogEntry(evt);

    // Track active proxies for Schedule rows.
    const inner = evt.payload?.kind;
    if (inner === "proxy_started") {
      const pid = evt.payload.proxy_id;
      if (pid) {
        activeProxies.set(pid, {
          engineName: evt.payload.engine_name || null,
        });
      }
    } else if (inner === "proxy_ended") {
      const pid = evt.payload.proxy_id;
      if (pid) activeProxies.delete(pid);
    } else if (
      evt.kind === "tournament_status" ||
      inner === "done" || inner === "stopped"
    ) {
      activeProxies.clear();
    }

    if (added) renderEventLog();
    renderSchedule();

    // Status changes and game finishes are good triggers to refresh
    // standings authoritatively.
    if (
      evt.kind === "tournament_status" ||
      inner === "game_finished" ||
      inner === "done" ||
      inner === "stopped"
    ) {
      refresh();
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
        if (!e.kind?.startsWith("tournament_")) continue;
        if (addLogEntry(e)) added = true;
      }
      if (added) renderEventLog();
    } catch (e) {
      log?.(`event backfill failed: ${e.message}`);
    }
  }
  backfillEvents();

  // Periodic poll: events should drive most updates, but a poll catches
  // server-restart catch-up windows and PGN-only changes (e.g. the
  // server's pgn_stats picks up games we missed via WS).
  pollTimer = window.setInterval(() => {
    if (detail?.status === "running") refresh();
  }, POLL_INTERVAL_MS);

  refresh();

  // ---- Tear-down --------------------------------------------------------

  let liveWatcherAttached = false;

  function onLiveGameClosed() {
    const allStandardClosed = Object.values(windows).every((w) => w === null);
    if (allStandardClosed && getLiveWindows().length === 0) finalize();
  }

  function finalize() {
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
    for (const k of Object.keys(windows)) {
      if (windows[k]) {
        windows[k].close(true); // skip the onclose callback's tearDown loop
        windows[k] = null;
      }
    }
    closeAllLiveGames();
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
      wb.resize(w, h).move(left + col * w, top + row * h);
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

  const workspace = { close, tile, cascade, closeAll, focus, hide, show, ensureWindows, tournamentId: tournament.id };
  activeWorkspace = workspace;
  return workspace;
}

export function getActiveWorkspace() {
  return activeWorkspace;
}
