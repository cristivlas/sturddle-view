// Tournament workspace: three WinBox windows (Standings, Schedule, Event log).
// Slice 9c adds the on-demand Live Game window: clicking an in-progress
// row in the Schedule subscribes to one engine's proxy stream and
// renders the position from that engine's POV.

import { closeAllLiveGames, openLiveGameWindow } from "./tournament-live-game.js";
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


export function openTournamentWorkspace({ api, events, log, token, tournament }) {
  // Close any prior workspace (single-active model).
  if (activeWorkspace) {
    activeWorkspace.close();
    activeWorkspace = null;
  }

  const layout = loadLayout();
  let detail = null;
  const eventLog = [];
  let pollTimer = null;
  let unsubscribe = null;
  // game_id -> {proxies: [pA, pB], engineNames: [...?]}
  const activeGames = new Map();

  // ---- Window construction ----------------------------------------------

  const standingsBody = document.createElement("div");
  standingsBody.className = "wb-standings";
  standingsBody.innerHTML = `<div class="wb-empty">Loading…</div>`;

  const scheduleBody = document.createElement("div");
  scheduleBody.className = "wb-schedule";
  scheduleBody.innerHTML = `<div class="wb-empty">Loading…</div>`;

  const logBody = document.createElement("div");
  logBody.className = "wb-eventlog";
  logBody.innerHTML = `<ul class="wb-eventlog-list"></ul>`;

  function makeBox(key, title, body) {
    const cfg = layout[key];
    const wb = new WinBox({
      title,
      x: cfg.x,
      y: cfg.y,
      width: cfg.width,
      height: cfg.height,
      mount: body,
      class: "sturddle-wb",
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

  const windows = {
    standings: makeBox("standings", `${tournament.name} — Standings`, standingsBody),
    schedule:  makeBox("schedule",  `${tournament.name} — Schedule`,  scheduleBody),
    log:       makeBox("log",       `${tournament.name} — Event log`, logBody),
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
    // each finished game). In-progress games: from `game_paired`
    // events (Slice 9b) — each becomes a row whose proxies the user
    // can click on to attach a Live Game window.
    const finished = (detail && detail.games) || [];
    const inProgress = [...activeGames.entries()];
    if (finished.length === 0 && inProgress.length === 0) {
      scheduleBody.innerHTML = `<div class="wb-empty">No games yet.</div>`;
      return;
    }
    scheduleBody.innerHTML = `<ul class="wb-sched-list"></ul>`;
    const list = scheduleBody.querySelector(".wb-sched-list");

    // Completed first (top), then in-progress.
    for (const g of finished) {
      const li = document.createElement("li");
      li.innerHTML = `
        <span class="wb-sched-icon">✓</span>
        ${escape(g.white)} – ${escape(g.black)}
        <span class="wb-sched-result">${escape(g.result)}</span>
      `;
      list.appendChild(li);
    }
    for (const [gid, g] of inProgress) {
      const li = document.createElement("li");
      li.className = "wb-sched-live";
      li.innerHTML = `
        <span class="wb-sched-icon">▶</span>
        <span class="wb-sched-game">game ${escape(gid)}</span>
        <span class="wb-sched-attach muted">attach:</span>
      `;
      const attachWrap = document.createElement("span");
      attachWrap.className = "wb-sched-attach-buttons";
      g.proxies.forEach((pid, i) => {
        const btn = document.createElement("button");
        btn.className = "wb-sched-attach-btn";
        btn.textContent = `engine ${i + 1}`;
        btn.title = pid;
        btn.addEventListener("click", () => {
          openLiveGameWindow({
            proxyId: pid,
            label: `${tournament.name} — game ${gid} · engine ${i + 1}`,
            token,
          });
        });
        attachWrap.appendChild(btn);
      });
      li.appendChild(attachWrap);
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
    renderStandings();
    renderSchedule();
  }

  function pushEvent(evt) {
    if (!evt) return;
    if (!evt.kind?.startsWith("tournament_")) return;
    // Only events for *our* tournament — the orchestrator stamps
    // tournament_id into payloads on the server side.
    const tid = evt.payload?.tournament_id;
    if (tid && tid !== tournament.id) return;

    const ts = new Date().toISOString().substring(11, 19);
    eventLog.push({ ts, kind: evt.kind, payload: evt.payload });
    if (eventLog.length > EVENT_LOG_LIMIT) eventLog.shift();

    // Slice 9c: track active games for the Schedule "in progress" rows.
    const inner = evt.payload?.kind;
    if (inner === "game_paired") {
      const gid = evt.payload.game_id;
      const proxies = evt.payload.proxies || [];
      if (gid && proxies.length === 2) {
        activeGames.set(gid, { proxies });
      }
    } else if (inner === "proxy_ended") {
      const ended = evt.payload.proxy_id;
      // Drop any active game whose proxies include the ended one.
      for (const [gid, g] of activeGames) {
        if (g.proxies.includes(ended)) activeGames.delete(gid);
      }
    } else if (
      evt.kind === "tournament_status" ||
      inner === "done" || inner === "stopped"
    ) {
      // Tournament ended; clear in-progress.
      activeGames.clear();
    }

    renderEventLog();
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

  unsubscribe = events.on(pushEvent);

  // Periodic poll: events should drive most updates, but a poll catches
  // server-restart catch-up windows and PGN-only changes (e.g. the
  // server's pgn_stats picks up games we missed via WS).
  pollTimer = window.setInterval(() => {
    if (detail?.status === "running") refresh();
  }, POLL_INTERVAL_MS);

  refresh();

  // ---- Tear-down --------------------------------------------------------

  function tearDown() {
    if (pollTimer != null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
    if (unsubscribe) {
      unsubscribe();
      unsubscribe = null;
    }
    closeAllLiveGames();
    if (activeWorkspace === workspace) activeWorkspace = null;
  }

  function close() {
    for (const k of Object.keys(windows)) {
      if (windows[k]) {
        windows[k].close(true); // skip the onclose callback's tearDown loop
        windows[k] = null;
      }
    }
    tearDown();
  }

  const workspace = { close, tournamentId: tournament.id };
  activeWorkspace = workspace;
  return workspace;
}


export function closeActiveWorkspace() {
  if (activeWorkspace) {
    activeWorkspace.close();
    activeWorkspace = null;
  }
}
