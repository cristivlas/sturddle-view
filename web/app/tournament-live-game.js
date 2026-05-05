// Live game window: one engine's proxy stream → board from that engine's POV.
// "Attach to engine, not to game" — opponent POV needs a second window.
// Renders: board (server-computed FEN from `position`), eval/depth/PV from
// this engine's `info`, clocks from `go wtime/btime`, last bestmove highlight.

import { mountBoard } from "./board.js";
import { flashWindow } from "./wb-utils.js";

const liveWindows = new Map(); // proxy_id -> WinBox instance

const LIVE_MIN_BOARD = 120; // px — smallest usable board side
const LIVE_CLOCK_H   = 36;  // px — one clock row (font 16px + padding)
const LIVE_EVAL_H    = 24;  // px — eval + PV rows collapsed
const LIVE_WINBOX_TITLE = 35; // px — WinBox title bar
const LIVE_GAP       = 6;   // px — flex gap between sections

const LIVE_MIN_WIDTH  = LIVE_MIN_BOARD;
const LIVE_MIN_HEIGHT = LIVE_WINBOX_TITLE + LIVE_CLOCK_H * 2 + LIVE_MIN_BOARD
                      + LIVE_EVAL_H + LIVE_GAP * 3;


export function openLiveGameWindow({ proxyId, label, token, top = 0, left = 0, boardStyle = null }) {
  // If a window for this proxy is already open, focus it instead of
  // opening a duplicate.
  const existing = liveWindows.get(proxyId);
  if (existing) {
    if (existing.min) existing.restore();
    existing.focus();
    flashWindow(existing);
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
      <span class="lg-eval-tbhits muted"></span>
    </div>
    <div class="lg-pv muted"></div>
    <div class="lg-status muted">connecting…</div>
  `;

  const boardHost = body.querySelector(".lg-board");
  const board = mountBoard({
    element: boardHost,
    styleId: boardStyle,
    onMove: () => {}, // read-only — moves come from the server.
  });

  const evalScoreEl = body.querySelector(".lg-eval-score");
  const evalDepthEl = body.querySelector(".lg-eval-depth");
  const evalTbhitsEl = body.querySelector(".lg-eval-tbhits");
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
    minwidth: LIVE_MIN_WIDTH,
    minheight: LIVE_MIN_HEIGHT,
    x: `${20 + (idx * 4)}%`,
    y: `${5 + (idx * 4)}%`,
    top,
    left,
    mount: body,
    class: "sturddle-wb sturddle-wb-live no-full",
  });
  if (top > 0 && wb.y < top) wb.move(wb.x, top);
  if (left > 0 && wb.x < left) wb.move(left, wb.y);
  liveWindows.set(proxyId, wb);
  requestAnimationFrame(() => flashWindow(wb));

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

  let ws = null;
  let engineColor = null;
  let timerInterval = null;
  let activeDeadline = 0;
  let currentFen = null;
  let positionGen = 0;
  let lastWtime = null;
  let lastBtime = null;

  wb.onclose = () => {
    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
    if (ws) try { ws.close(); } catch { /* */ }
    ro.disconnect();
    liveWindows.delete(proxyId);
    window.dispatchEvent(new CustomEvent("sturddle:livegame-closed"));
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

  function stopTimer() {
    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
    clockTopEl.classList.remove("active");
    clockBottomEl.classList.remove("active");
  }

  ws.addEventListener("close", () => {
    stopTimer();
    try { wb.close(); } catch { /* */ }
  });

  ws.addEventListener("error", () => {
    statusEl.textContent = "connection error";
    stopTimer();
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
      stopTimer();
      try { ws.close(); } catch { /* */ }
      return;
    }
    const parsed = msg.parsed;
    if (!parsed) return;
    if (msg.paired) {
      handlePairedParsed(parsed, msg.thinking_side);
      return;
    }
    handleParsed(parsed);
  });

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

  async function applyBestMove(uciMove) {
    const fen = currentFen;
    const gen = positionGen;
    try {
      const headers = { "Content-Type": "application/json" };
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const res = await fetch("/api/chess/apply-move", {
        method: "POST",
        headers,
        body: JSON.stringify({ fen, move: uciMove }),
      });
      if (!res.ok) return;
      const { fen: newFen } = await res.json();
      // Discard if a newer `position` message arrived while the fetch was in flight.
      if (positionGen === gen) {
        currentFen = newFen;
        board.setPosition(newFen, uciMove);
        board.clearArrows();
      }
    } catch { /* non-fatal: next position message will correct the board */ }
  }

  function handleParsed(p) {
    switch (p.kind) {
      case "position":
        if (p.fen) {
          positionGen++;
          currentFen = p.fen;
          const turn = p.fen.split(" ")[1];
          const color = turn === "b" ? "black" : "white";
          if (color !== engineColor) {
            setEngineColor(color);
            board.setSide(color);
          }
          board.setPosition(p.fen, p.last_move || null);
          board.clearArrows();
        }
        break;
      case "info":
        renderEval(p);
        break;
      case "go":
        lastWtime = p.wtime ?? lastWtime;
        lastBtime = p.btime ?? lastBtime;
        updateClocks(p.wtime, p.btime);
        clockTopEl.classList.remove("active");
        clockBottomEl.classList.toggle("active", !!engineColor);
        if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
        if (engineColor && p.wtime != null && p.btime != null) {
          const startMs = engineColor === "white" ? p.wtime : p.btime;
          activeDeadline = Date.now() + startMs;
          const tick = () => {
            const remaining = Math.max(0, activeDeadline - Date.now());
            bottomTimeEl.textContent = formatMs(remaining);
            if (remaining === 0 && timerInterval) {
              clearInterval(timerInterval);
              timerInterval = null;
            }
          };
          tick();
          timerInterval = setInterval(tick, 100);
        }
        break;
      case "bestmove":
        if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
        clockBottomEl.classList.remove("active");
        clockTopEl.classList.toggle("active", !!engineColor);
        if (currentFen && p.move) applyBestMove(p.move);
        if (engineColor && lastWtime != null && lastBtime != null) {
          const oppMs = engineColor === "white" ? lastBtime : lastWtime;
          activeDeadline = Date.now() + oppMs;
          const tick = () => {
            const remaining = Math.max(0, activeDeadline - Date.now());
            topTimeEl.textContent = formatMs(remaining);
            if (remaining === 0 && timerInterval) {
              clearInterval(timerInterval);
              timerInterval = null;
            }
          };
          tick();
          timerInterval = setInterval(tick, 100);
        }
        break;
    }
  }

  function handlePairedParsed(p, thinkingSide) {
    // Paired info: from the opposite-color engine. Only ``info`` is
    // forwarded; render its first-PV move as the opponent arrow.
    if (p.kind !== "info" || !p.pv || !p.pv.length) return;
    if (engineColor && thinkingSide === engineColor) return;
    const m = p.pv[0];
    if (m && m.length >= 4) board.setOpponentArrow(m.slice(0, 2), m.slice(2, 4));
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
    evalDepthEl.textContent = p.depth != null
      ? (p.seldepth != null ? `d${p.depth}/${p.seldepth}` : `d${p.depth}`)
      : "";
    evalTbhitsEl.textContent = p.tbhits ? `tb ${p.tbhits}` : "";
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

export function isLiveWindowOpen(proxyId) {
  return liveWindows.has(proxyId);
}

export function closeAllLiveGames() {
  for (const wb of liveWindows.values()) {
    try { wb.close(true); } catch { /* */ }
  }
  liveWindows.clear();
}
