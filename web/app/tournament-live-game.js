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
    <div class="lg-clocks">
      <div class="lg-clock lg-clock-white"><span class="lg-clock-label">White</span><span class="lg-clock-value">—</span></div>
      <div class="lg-clock lg-clock-black"><span class="lg-clock-label">Black</span><span class="lg-clock-value">—</span></div>
    </div>
    <div class="lg-board"></div>
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
  const whiteClockEl = body.querySelector(".lg-clock-white .lg-clock-value");
  const blackClockEl = body.querySelector(".lg-clock-black .lg-clock-value");
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
  function constrainAndResize() {
    boardHost.style.width = "";
    const h = boardHost.clientHeight;
    const w = boardHost.clientWidth;
    if (h > 0 && h < w) boardHost.style.width = `${h}px`;
    board.forceResize();
  }
  const ro = new ResizeObserver(constrainAndResize);
  ro.observe(body);
  requestAnimationFrame(constrainAndResize);

  // ws on connect — set up after construction to avoid TDZ.
  let ws = null;
  wb.onclose = () => {
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

  function handleParsed(p) {
    switch (p.kind) {
      case "position":
        if (p.fen) {
          // Set orientation from the first position line. fastchess
          // sends `position ... moves ...` with side-to-move = the
          // engine receiving the line — so its color in the game.
          // (Waiting for `go` would delay orientation by one full
          // opponent think-time when the user attaches mid-game.)
          if (!orientationSet) {
            const turn = p.fen.split(" ")[1];
            board.setSide(turn === "b" ? "black" : "white");
            orientationSet = true;
          }
          board.setPosition(p.fen, p.last_move || null);
        }
        break;
      case "info":
        renderEval(p);
        break;
      case "go":
        if (p.wtime != null) whiteClockEl.textContent = formatMs(p.wtime);
        if (p.btime != null) blackClockEl.textContent = formatMs(p.btime);
        break;
      case "bestmove":
        // Could highlight the move; the next position line will
        // propagate through setPosition anyway.
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
