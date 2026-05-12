// Live game window: one engine's proxy stream -> board from that engine's POV.
// "Attach to engine, not to game" -- opponent POV needs a second window.
// Renders: board (server-computed FEN from `position`), eval/depth/PV from
// this engine's `info`, clocks from `go wtime/btime`, last bestmove highlight.

import { mountBoard } from "./board.js";
import { confirm, reportError } from "./dialogs.js";
import { isPlayInProgress } from "./perspectives/play.js";
import { flashWindow } from "./wb-utils.js";

const REPLAY_DISCARD_MSG = "Discard your in-progress game and replay this tournament game?";

async function replayTournamentGame({ tournamentId, gameN, token }) {
  if (isPlayInProgress()) {
    const ok = await confirm({
      message: REPLAY_DISCARD_MSG,
      okLabel: "Replay",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
  }
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let pgn;
  try {
    const res = await fetch(`/api/tournaments/${encodeURIComponent(tournamentId)}/games/${gameN}/pgn`, { headers });
    if (!res.ok) throw new Error(`fetch pgn -> ${res.status}`);
    pgn = (await res.json()).pgn;
  } catch (e) {
    reportError(null, "Replay: fetch failed", e);
    return;
  }
  try {
    const res = await fetch("/game/import", {
      method: "POST",
      headers,
      body: JSON.stringify({ text: pgn, format: "pgn" }),
    });
    if (!res.ok) throw new Error(`import -> ${res.status}`);
  } catch (e) {
    reportError(null, "Replay: import failed", e);
    return;
  }
  window.dispatchEvent(new CustomEvent("sturddle:activate-perspective", { detail: { id: "play" } }));
}

// Flip to true to re-enable verbose [WATCH] tracing for debugging
// intermittent click-watch failures. Errors are always logged.
export const DEBUG_WATCH = false;

const liveWindows = new Map(); // windowKey -> WinBox instance

// Row heights are duplicated as `min-height` on .wb-livegame .lg-eval /
// .lg-pv / .lg-status in styles.css so empty rows still hold space
// before pairing data arrives. Keep the two in sync.
const LIVE_MIN_BOARD    = 200; // px -- smallest usable board side
const LIVE_CLOCK_H      = 36;  // px -- one clock row (font 16px + padding)
const LIVE_EVAL_H       = 18;  // px -- eval row (0.8125rem * 1.4 line-height)
const LIVE_PV_H         = 17;  // px -- pv row (0.6875rem * 1.4 line-height, +2px bottom padding)
const LIVE_WINBOX_TITLE = 35;  // px -- WinBox title bar
const LIVE_GAP          = 4;   // px -- flex gap between sections

const ARROW_MIN_TIME_MS = 250; // skip arrow if side-to-move has less time than this

export const LIVE_MIN_WIDTH  = LIVE_MIN_BOARD;
// 7 flex children: pv-top, eval-top, clock-top, board, clock-bottom, eval-bottom, pv-bottom -- 6 gaps.
export const LIVE_MIN_HEIGHT = LIVE_WINBOX_TITLE + LIVE_PV_H * 2 + LIVE_EVAL_H * 2 + LIVE_CLOCK_H * 2 + LIVE_MIN_BOARD + LIVE_GAP * 6;


// Gap (px) between the avoid-rect and the new window when displacing.
const AVOID_GAP = 8;

function rectsOverlap(a, b) {
  return !(a.x + a.w <= b.x || b.x + b.w <= a.x ||
           a.y + a.h <= b.y || b.y + b.h <= a.y);
}

// Try to displace `wb` so it doesn't overlap `avoid`. `avoid` is
// {x,y,w,h} in viewport pixels. Tries right, below, left, above in
// order; first candidate that fits in the viewport wins. `cascade`
// adds a diagonal pixel offset so multiple windows stagger rather
// than stack at the same position. Leaves the window in place if
// none fit.
function avoidOverlap(wb, avoid, top, left, cascade = 0) {
  const cur = { x: wb.x, y: wb.y, w: wb.width, h: wb.height };
  if (!rectsOverlap(cur, avoid)) return;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const candidates = [
    { x: avoid.x + avoid.w + AVOID_GAP, y: cur.y },              // right
    { x: cur.x, y: avoid.y + avoid.h + AVOID_GAP },              // below
    { x: avoid.x - cur.w - AVOID_GAP, y: cur.y },                // left
    { x: cur.x, y: avoid.y - cur.h - AVOID_GAP },                // above
  ];
  for (const c of candidates) {
    const cx = c.x + cascade, cy = c.y + cascade;
    if (cx >= left && cy >= top &&
        cx + cur.w <= vw && cy + cur.h <= vh) {
      wb.move(cx, cy);
      return;
    }
  }
}

export function openLiveGameWindow({ proxyId, gameId = null, windowKey = gameId ?? proxyId, label, engineName, token, tournamentId = null, top = 0, left = 0, boardStyle = null, avoidRect = null, initialRect = null, onAfterRestore = null, getSlotSize = null }) {
  if (DEBUG_WATCH) console.log("[WATCH] openLiveGameWindow", { proxyId, gameId, windowKey, label });
  if (!windowKey) {
    console.error("[WATCH] no windowKey -- need at least one of proxyId/gameId", { proxyId, gameId });
    return;
  }
  // If a window for this key is already open, focus it instead of
  // opening a duplicate.
  const existing = liveWindows.get(windowKey);
  if (existing) {
    if (DEBUG_WATCH) console.log("[WATCH] window already open -- focusing", { windowKey });
    if (existing.min) existing.restore();
    existing.focus();
    flashWindow(existing);
    return { wb: existing, alreadyOpen: true };
  }

  const body = document.createElement("div");
  body.className = "wb-livegame";
  body.innerHTML = `
    <div class="lg-pv lg-pv-top muted"></div>
    <div class="lg-eval lg-eval-top">
      <span class="lg-eval-score lg-eval-score-top"></span>
      <span class="lg-eval-depth lg-eval-depth-top muted"></span>
      <span class="lg-eval-tbhits lg-eval-tbhits-top muted"></span>
    </div>
    <div class="clock-row lg-clock-top">
      <span class="clock-name lg-top-name">--</span>
      <span class="clock-time lg-top-time">--</span>
    </div>
    <div class="lg-board">
      <div class="lg-result-overlay" hidden>
        <div class="lg-result-score"></div>
        <div class="lg-result-termination"></div>
        <button type="button" class="lg-result-replay" hidden>Replay</button>
      </div>
    </div>
    <div class="clock-row lg-clock-bottom">
      <span class="clock-name lg-bottom-name">--</span>
      <span class="clock-time lg-bottom-time">--</span>
    </div>
    <div class="lg-eval lg-eval-bottom">
      <span class="lg-eval-score lg-eval-score-bottom"></span>
      <span class="lg-eval-depth lg-eval-depth-bottom muted"></span>
      <span class="lg-eval-tbhits lg-eval-tbhits-bottom muted"></span>
    </div>
    <div class="lg-pv lg-pv-bottom muted"></div>
  `;

  const boardHost = body.querySelector(".lg-board");
  const board = mountBoard({
    element: boardHost,
    styleId: boardStyle,
    onMove: () => {}, // read-only -- moves come from the server.
  });

  const evalScoreEl = body.querySelector(".lg-eval-score-bottom");
  const evalDepthEl = body.querySelector(".lg-eval-depth-bottom");
  const evalTbhitsEl = body.querySelector(".lg-eval-tbhits-bottom");
  const pvEl = body.querySelector(".lg-pv-bottom");
  const oppEvalScoreEl = body.querySelector(".lg-eval-score-top");
  const oppEvalDepthEl = body.querySelector(".lg-eval-depth-top");
  const oppEvalTbhitsEl = body.querySelector(".lg-eval-tbhits-top");
  const oppPvEl = body.querySelector(".lg-pv-top");
  const clockTopEl = body.querySelector(".lg-clock-top");
  const clockBottomEl = body.querySelector(".lg-clock-bottom");
  const topNameEl = body.querySelector(".lg-top-name");
  const bottomNameEl = body.querySelector(".lg-bottom-name");
  const topTimeEl = body.querySelector(".lg-top-time");
  const bottomTimeEl = body.querySelector(".lg-bottom-time");
  const resultOverlayEl = body.querySelector(".lg-result-overlay");
  const resultScoreEl = body.querySelector(".lg-result-score");
  const resultTerminationEl = body.querySelector(".lg-result-termination");
  const replayBtnEl = body.querySelector(".lg-result-replay");

  const debugTag = gameId ?? proxyId ?? "";
  const titleWithTag = debugTag ? `${label} [${debugTag}]` : label;
  // Caller-provided slot wins; otherwise fall back to a cascading default.
  const idx = liveWindows.size;
  const initialWidth = initialRect?.w
    ?? Math.max(Math.round(window.innerWidth * 0.20), LIVE_MIN_WIDTH);
  const initialHeight = initialRect?.h ?? null;
  const wb = new WinBox({
    title: titleWithTag,
    width: initialWidth,
    ...(initialHeight ? { height: initialHeight } : {}),
    minwidth: LIVE_MIN_WIDTH,
    minheight: LIVE_MIN_HEIGHT,
    x: initialRect ? initialRect.x : `${20 + (idx * 4)}%`,
    y: initialRect ? initialRect.y : `${5 + (idx * 4)}%`,
    top,
    left,
    mount: body,
    class: gameId
      ? "sturddle-wb sturddle-wb-live sturddle-wb-live-game no-full"
      : "sturddle-wb sturddle-wb-live sturddle-wb-live-proxy no-full",
  });
  let slotSizeSnap = null;
  if (getSlotSize) wb.onminimize = () => { slotSizeSnap = getSlotSize(); };
  if (onAfterRestore) wb.onrestore = () => {
    if (slotSizeSnap) { wb.resize(slotSizeSnap.w, slotSizeSnap.h); slotSizeSnap = null; }
    onAfterRestore(wb);
  };

  const clampToViewport = () => {
    const maxX = Math.max(left, window.innerWidth  - wb.width);
    const maxY = Math.max(top,  window.innerHeight - wb.height);
    const cx = Math.min(Math.max(wb.x, left), maxX);
    const cy = Math.min(Math.max(wb.y, top),  maxY);
    if (cx !== wb.x || cy !== wb.y) wb.move(cx, cy);
  };
  clampToViewport();
  // Clamp height so the window can't grow taller than the board needs:
  // a portrait-stretched window wastes space and looks broken.
  wb.onresize = (w, h) => {
    const maxH = w + LIVE_WINBOX_TITLE + FIXED_FULL;
    if (h > maxH) wb.resize(w, maxH);
  };
  if (avoidRect) avoidOverlap(wb, avoidRect, top, left, idx * 24);
  // WinBox doesn't expose its config minwidth/minheight as instance fields;
  // stash them so the workspace's tile() can clamp.
  wb.svMinWidth = LIVE_MIN_WIDTH;
  wb.svMinHeight = LIVE_MIN_HEIGHT;
  liveWindows.set(windowKey, wb);
  // Result-banner upgrade on game_reconciled (workspace dispatches).
  // Captured here so the Replay button knows which PGN slice to fetch.
  let reconciledGameN = null;
  let replayInFlight = false;
  const onReconciled = (e) => {
    if (gameId && e.detail?.pairId === gameId) {
      showResult(e.detail.result, e.detail.termination);
      if (e.detail.gameN != null && tournamentId) {
        reconciledGameN = e.detail.gameN;
        replayBtnEl.hidden = false;
      }
    }
  };
  if (gameId) {
    window.addEventListener("sturddle:reconciled", onReconciled);
    replayBtnEl.addEventListener("click", async () => {
      if (replayInFlight || reconciledGameN == null || !tournamentId) return;
      replayInFlight = true;
      replayBtnEl.disabled = true;
      try {
        await replayTournamentGame({ tournamentId, gameN: reconciledGameN, token });
      } finally {
        replayInFlight = false;
        replayBtnEl.disabled = false;
      }
    });
  }
  requestAnimationFrame(() => flashWindow(wb));

  // Compute target board size deterministically from the body's
  // dimensions and the known fixed-row heights. Reading the board
  // host's measured size during a resize creates a feedback loop with
  // cm-chessboard's internal SVG sizing, which is what produced the
  // narrow-board-after-restore bug.
  //
  // Fixed (non-board) rows: 2x pv, 2x eval, 2x clock + 6 gaps (7 children total).
  // In compact mode (body.clientHeight < 280) the .lg-pv rows are
  // display:none, so those drop out.
  const FIXED_FULL = LIVE_PV_H * 2 + LIVE_EVAL_H * 2 + LIVE_CLOCK_H * 2 + LIVE_GAP * 6;
  const FIXED_COMPACT = LIVE_EVAL_H * 2 + LIVE_CLOCK_H * 2 + LIVE_GAP * 4;

  function constrainAndResize() {
    const compact = body.clientHeight < 280;
    body.classList.toggle("lg-compact", compact);
    const fixed = compact ? FIXED_COMPACT : FIXED_FULL;
    const sz = Math.max(0, Math.min(body.clientHeight - fixed, body.clientWidth));
    boardHost.style.width = `${sz}px`;
    boardHost.style.height = `${sz}px`;
    for (const el of [clockTopEl, clockBottomEl]) {
      el.style.width = `${sz}px`;
      el.style.margin = "0 auto";
    }
    board.forceResize();
  }
  const ro = new ResizeObserver(constrainAndResize);
  ro.observe(body);
  requestAnimationFrame(constrainAndResize);

  let ws = null;
  let engineColor = null;
  let opponentName = null;
  let timerInterval = null;
  let activeDeadline = 0;
  let currentFen = null;
  let positionGen = 0;
  let lastWtime = null;
  let lastBtime = null;

  let wbClosed = false;
  wb.onclose = () => {
    wbClosed = true;
    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
    if (ws) try { ws.close(); } catch { /* */ }
    ro.disconnect();
    if (gameId) window.removeEventListener("sturddle:reconciled", onReconciled);
    liveWindows.delete(windowKey);
    window.dispatchEvent(new CustomEvent("sturddle:livegame-closed"));
    return false;
  };

  // Open WS subscription.
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const tokenQ = token ? `?token=${encodeURIComponent(token)}` : "";
  const wsTarget = gameId
    ? `game/${encodeURIComponent(gameId)}`
    : `proxy/${encodeURIComponent(proxyId)}`;
  const url = `${proto}//${location.host}/ws/tournament/${wsTarget}${tokenQ}`;
  if (DEBUG_WATCH) console.log("[WATCH] ws connect", { windowKey, url });
  ws = new WebSocket(url);

  ws.addEventListener("open", () => {
    if (DEBUG_WATCH) console.log("[WATCH] ws open", { windowKey });
  });

  function stopTimer() {
    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
    clockTopEl.classList.remove("active");
    clockBottomEl.classList.remove("active");
  }

  ws.addEventListener("close", (e) => {
    if (DEBUG_WATCH) console.log("[WATCH] ws close", { windowKey, code: e.code, reason: e.reason, wasClean: e.wasClean });
    stopTimer();
    // User-initiated close already tore the window down; calling
    // wb.close() again here corrupts WinBox's focus tracker and breaks
    // click-to-front globally.
    if (wbClosed) return;
    try { wb.close(); } catch { /* */ }
  });

  ws.addEventListener("error", (e) => {
    console.error("[WATCH] ws error", { windowKey, event: e });
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
      // Late-attach race: sentinel arrived before any position. The
      // window has nothing to show -- auto-close instead of leaving a
      // startpos banner behind.
      if (gameId && currentFen === null) {
        stopTimer();
        try { ws.close(); } catch { /* */ }
        try { wb.close(); } catch { /* */ }
        return;
      }
      // game-id WS sends result + termination on dissolution => banner.
      // proxy-id WS sends bare {ended:true} when the engine process
      // exits (typically tournament shutdown) => quiet status text.
      if (msg.result) {
        showResult(msg.result, msg.termination);
      } else {
      }
      stopTimer();
      wbClosed = true; // suppress wb.close() in the WS close handler
      try { ws.close(); } catch { /* */ }
      return;
    }
    const parsed = msg.parsed;
    if (!parsed) return;
    const isOpponent = msg.paired || (gameId && msg.proxy_id && msg.proxy_id !== proxyId);
    if (isOpponent) {
      if (msg.engine_name) setOpponentName(msg.engine_name);
      handlePairedParsed(parsed, msg.thinking_side);
      return;
    }
    handleParsed(parsed);
  });

  function setEngineColor(color) {
    engineColor = color;
    const oppColor = color === "white" ? "black" : "white";
    bottomNameEl.textContent = engineName || (color === "white" ? "White" : "Black");
    if (!opponentName) topNameEl.textContent = oppColor === "white" ? "White" : "Black";
    clockBottomEl.dataset.color = color;
    clockTopEl.dataset.color = oppColor;
  }

  function setOpponentName(name) {
    if (!name || name === opponentName) return;
    opponentName = name;
    topNameEl.textContent = name;
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
      if (res.status === 204) { console.warn("late bestmove skipped:", uciMove); return; }
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

  let resultPainted = false;
  function showResult(result, termination) {
    // result is "1-0" | "0-1" | "1/2-1/2" | "*" | null/undefined.
    // The per-pair WS sentinel and the tournament-wide game_reconciled
    // arrive in nondeterministic order; if real values landed first,
    // ignore a subsequent "*" downgrade.
    const isReal = result && result !== "*";
    if (resultPainted && !isReal) return;
    if (isReal) resultPainted = true;
    const score = isReal ? result : "game ended";
    const term = (termination && termination !== "unknown") ? termination : "";
    resultScoreEl.textContent = score;
    resultTerminationEl.textContent = term;
    resultOverlayEl.hidden = false;
  }


  function pvArrowMove(p) {
    if (!p.pv || !p.pv.length) return null;
    if (!p.time || p.time < ARROW_MIN_TIME_MS) return null;
    const m = p.pv[0];
    return m && m.length >= 4 ? m : null;
  }

  function handlePairedParsed(p, thinkingSide) {
    // Paired info: opposite-color engine's thinking. Mirror the same
    // eval/PV/arrow rendering as own-side, into the top (opponent) row.
    if (p.kind !== "info") return;
    if (engineColor && thinkingSide === engineColor) return;
    renderOpponentEval(p);
    const m = pvArrowMove(p);
    if (m) board.setOpponentArrow(m.slice(0, 2), m.slice(2, 4));
  }

  function renderOpponentEval(p) {
    let scoreText = "--";
    if (p.score_cp != null) {
      const cp = p.score_cp;
      scoreText = (cp >= 0 ? "+" : "") + (cp / 100).toFixed(2);
    } else if (p.score_mate != null) {
      scoreText = `M${p.score_mate}`;
    }
    oppEvalScoreEl.textContent = scoreText;
    oppEvalDepthEl.textContent = p.depth != null
      ? (p.seldepth != null ? `d${p.depth}/${p.seldepth}` : `d${p.depth}`)
      : "";
    oppEvalTbhitsEl.textContent = p.tbhits ? `tb ${p.tbhits}` : "";
    if (p.pv && p.pv.length) {
      oppPvEl.textContent = p.pv.slice(0, 12).join(" ");
    }
  }

  function renderEval(p) {
    let scoreText = "--";
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
      const m = pvArrowMove(p);
      if (m) board.setArrow(m.slice(0, 2), m.slice(2, 4));
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
    wb,
    alreadyOpen: false,
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

// Close all live windows on terminal/switch. Result/termination is
// always UNKNOWN today, so there's nothing to review post-game.
export function closeStaleLiveGames() {
  for (const [key, wb] of [...liveWindows.entries()]) {
    try { wb.close(true); } catch { /* */ }
    liveWindows.delete(key);
  }
}
