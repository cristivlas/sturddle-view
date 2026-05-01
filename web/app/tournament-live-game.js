// Live game window: subscribes to one engine's proxy stream and
// renders the board from that engine's POV. The user attaches by
// clicking a row in the Schedule window (Slice 9c).
//
// Per the spec's "attach to engine, not to game" model, this window
// shows ONE engine's perspective. To see the opponent's POV, the user
// opens a second live window for the other proxy.
//
// What's rendered:
//   - Board reconstructed from `position` lines (FEN computed
//     server-side; we just call setPosition).
//   - Eval / depth / PV from this engine's `info` lines.
//   - Both clocks from `go wtime / btime`.
//   - Last bestmove highlighted on the board.

import { mountBoard } from "./board.js";

const liveWindows = new Map(); // proxy_id -> WinBox instance


export function openLiveGameWindow({ proxyId, label, token, top = 0 }) {
  // If a window for this proxy is already open, focus it instead of
  // opening a duplicate.
  const existing = liveWindows.get(proxyId);
  if (existing) {
    existing.focus();
    return;
  }

  const body = document.createElement("div");
  body.className = "wb-livegame";
  body.innerHTML = `
    <div class="clock-row lg-clock-top">
      <span class="clock-name lg-top-name">—</span>
      <span class="clock-time lg-top-time">—</span>
    </div>
    <div class="lg-board"></div>
    <div class="clock-row lg-clock-bottom">
      <span class="clock-name lg-bottom-name">—</span>
      <span class="clock-time lg-bottom-time">—</span>
    </div>
    <div class="lg-eval">
      <span class="lg-eval-score">—</span>
      <span class="lg-eval-depth muted"></span>
    </div>
    <div class="lg-pv muted"></div>
    <div class="lg-status muted">connecting…</div>
  `;

  const boardHost = body.querySelector(".lg-board");
  const board = mountBoard({
    element: boardHost,
    onMove: () => {}, // read-only — moves come from the server.
  });

  const evalScoreEl = body.querySelector(".lg-eval-score");
  const evalDepthEl = body.querySelector(".lg-eval-depth");
  const pvEl = body.querySelector(".lg-pv");
  const clockTopEl = body.querySelector(".lg-clock-top");
  const clockBottomEl = body.querySelector(".lg-clock-bottom");
  const topNameEl = body.querySelector(".lg-top-name");
  const bottomNameEl = body.querySelector(".lg-bottom-name");
  const topTimeEl = body.querySelector(".lg-top-time");
  const bottomTimeEl = body.querySelector(".lg-bottom-time");
  const statusEl = body.querySelector(".lg-status");

  // Default WinBox layout for live windows. Cascade by index so multiple
  // windows don't fully overlap.
  const idx = liveWindows.size;
  const wb = new WinBox({
    title: label,
    width: "30%",
    height: "55%",
    x: `${20 + (idx * 4)}%`,
    y: `${5 + (idx * 4)}%`,
    top,
    mount: body,
    class: "sturddle-wb sturddle-wb-live",
  });
  if (top > 0 && wb.y < top) wb.move(wb.x, top);
  liveWindows.set(proxyId, wb);

  // Keep the board square and fitting the WinBox window on every resize.
  // Constrain clock rows to the same width so they align with board edges.
  function constrainAndResize() {
    for (const el of [clockTopEl, boardHost, clockBottomEl]) {
      el.style.width = "";
      el.style.margin = "";
    }
    body.classList.toggle("lg-compact", body.clientHeight < 280);
    const h = boardHost.clientHeight;
    const w = boardHost.clientWidth;
    if (h > 0 && h < w) {
      for (const el of [clockTopEl, boardHost, clockBottomEl]) {
        el.style.width = `${h}px`;
        el.style.margin = "0 auto";
      }
    }
    board.forceResize();
  }
  const ro = new ResizeObserver(constrainAndResize);
  ro.observe(body);
  requestAnimationFrame(constrainAndResize);

  // ws on connect — set up after construction to avoid TDZ.
  let ws = null;
  wb.onclose = () => {
    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
    if (ws) try { ws.close(); } catch { /* */ }
    ro.disconnect();
    liveWindows.delete(proxyId);
    return false;
  };

  // Open WS subscription.
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const tokenQ = token ? `?token=${encodeURIComponent(token)}` : "";
  const url = `${proto}//${location.host}/ws/tournament/proxy/${encodeURIComponent(proxyId)}${tokenQ}`;
  ws = new WebSocket(url);

  ws.addEventListener("open", () => {
    statusEl.textContent = "live";
  });

  ws.addEventListener("close", () => {
    statusEl.textContent = "ended";
  });

  ws.addEventListener("error", () => {
    statusEl.textContent = "connection error";
  });

  ws.addEventListener("message", (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg.ended) {
      statusEl.textContent = "ended";
      try { ws.close(); } catch { /* */ }
      return;
    }
    const parsed = msg.parsed;
    if (!parsed) return;
    handleParsed(parsed);
  });

  let orientationSet = false;
  let engineColor = null; // fixed on first position line
  let timerInterval = null;
  let activeMs = 0;

  function setEngineColor(color) {
    engineColor = color;
    const opp = color === "white" ? "Black" : "White";
    bottomNameEl.textContent = color === "white" ? "White" : "Black";
    topNameEl.textContent = opp;
  }

  function updateClocks(wtime, btime) {
    if (!engineColor || wtime == null || btime == null) return;
    bottomTimeEl.textContent = formatMs(engineColor === "white" ? wtime : btime);
    topTimeEl.textContent = formatMs(engineColor === "white" ? btime : wtime);
  }

  function handleParsed(p) {
    switch (p.kind) {
      case "position":
        if (p.fen) {
          const turn = p.fen.split(" ")[1];
          const color = turn === "b" ? "black" : "white";
          if (!orientationSet) {
            setEngineColor(color);
            board.setSide(color);
            orientationSet = true;
          }
          board.setPosition(p.fen, p.last_move || null);
          board.clearArrows();
        }
        break;
      case "info":
        renderEval(p);
        break;
      case "go":
        updateClocks(p.wtime, p.btime);
        clockTopEl.classList.remove("active");
        clockBottomEl.classList.toggle("active", !!engineColor);
        if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
        if (engineColor && p.wtime != null && p.btime != null) {
          activeMs = engineColor === "white" ? p.wtime : p.btime;
          timerInterval = setInterval(() => {
            activeMs = Math.max(0, activeMs - 100);
            bottomTimeEl.textContent = formatMs(activeMs);
            if (activeMs === 0) { clearInterval(timerInterval); timerInterval = null; }
          }, 100);
        }
        break;
      case "bestmove":
        if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
        clockBottomEl.classList.remove("active");
        clockTopEl.classList.toggle("active", !!engineColor);
        break;
    }
  }

  function renderEval(p) {
    let scoreText = "—";
    if (p.score_cp != null) {
      const cp = p.score_cp;
      scoreText = (cp >= 0 ? "+" : "") + (cp / 100).toFixed(2);
    } else if (p.score_mate != null) {
      scoreText = `M${p.score_mate}`;
    }
    evalScoreEl.textContent = scoreText;
    evalDepthEl.textContent = p.depth != null ? `d${p.depth}` : "";
    if (p.pv && p.pv.length) {
      pvEl.textContent = p.pv.slice(0, 12).join(" ");
      const m = p.pv[0];
      if (m && m.length >= 4) board.setArrow(m.slice(0, 2), m.slice(2, 4));
    }
  }

  function formatMs(ms) {
    if (ms < 0) return "0.0";
    const s = ms / 1000;
    if (s >= 60) {
      const m = Math.floor(s / 60);
      const r = (s - m * 60).toFixed(0);
      return `${m}:${r.padStart(2, "0")}`;
    }
    return s.toFixed(1);
  }

  return {
    close() {
      try { wb.close(); } catch { /* */ }
    },
  };
}


export function getLiveWindows() {
  return [...liveWindows.values()];
}

export function closeAllLiveGames() {
  for (const wb of liveWindows.values()) {
    try { wb.close(true); } catch { /* */ }
  }
  liveWindows.clear();
}
