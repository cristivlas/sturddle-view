// Live game window: one engine's proxy stream -> board from that engine's POV.
// "Attach to engine, not to game" -- opponent POV needs a second window.
// Renders: board (server-computed FEN from `position`), eval/depth/PV from
// this engine's `info`, clocks from `go wtime/btime`, last bestmove highlight.

import { mountBoard } from "./board.js";
import { confirm, reportError, toast } from "./dialogs.js";
import { isPlayInProgress, isViewing, isAnalyzing, getViewingHash, getViewingSummary } from "./perspectives/play.js";
import { confirmReplaceViewedGame } from "./import-position-dialog.js";
import { flashWindow } from "./wb-utils.js";
import { terminationPhrase } from "./format-termination.js";

async function replayTournamentGame({ tournamentId, gameN, token, pairId = null }) {
  const headers = { "Content-Type": "application/json" };
  let pgn, pgnHash, pgnSummary;
  try {
    const res = await fetch(`/api/tournaments/${encodeURIComponent(tournamentId)}/games/${gameN}/pgn`, { headers });
    if (!res.ok) throw new Error(`fetch pgn -> ${res.status}`);
    const rec = await res.json();
    pgn = rec.pgn;
    pgnHash = rec.hash ?? null;
    pgnSummary = rec.summary ?? null;
  } catch (e) {
    reportError(null, "Replay: fetch failed", e);
    return;
  }
  if (isPlayInProgress()) {
    const ok = await confirm({
      message: "Discard your in-progress game and replay this tournament game?",
      okLabel: "Replay",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
  } else if (isViewing()) {
    if (pgnHash && pgnHash === getViewingHash()) {
      // Same game already viewed: switch perspective and surface a
      // toast carrying the pair id. The toast doubles as a live
      // invariant check for the game_id-unification refactor -- if
      // the value looks wrong, the unification is broken.
      window.dispatchEvent(new CustomEvent("sturddle:activate-perspective", {
        detail: { id: "play" },
      }));
      if (pairId) toast(`Viewing ${pairId}`);
      return;
    }
    const ok = await confirmReplaceViewedGame({
      currentHash: getViewingHash(),
      currentSummary: getViewingSummary(),
      incomingHash: pgnHash,
      incomingSummary: pgnSummary,
      analysisRunning: isAnalyzing(),
    });
    if (!ok) return;
  }
  let importedGameId = null;
  try {
    const body = { text: pgn, format: "pgn" };
    if (pairId) body.game_id = pairId;
    const res = await fetch("/game/import", {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`import -> ${res.status}`);
    const data = await res.json();
    importedGameId = data?.game_id ?? null;
  } catch (e) {
    reportError(null, "Replay: import failed", e);
    return;
  }
  // Server-side invariant: when the client supplied a game_id, the
  // import response must echo it back exactly. Mismatch is a unification
  // bug, not a race -- surface it loudly.
  if (pairId && importedGameId && importedGameId !== pairId) {
    reportError(
      null,
      "Replay: server returned game_id != pair_id (unification bug)",
      new Error(`stored=${importedGameId} pair=${pairId}`),
    );
    return;
  }
  window.dispatchEvent(new CustomEvent("sturddle:activate-perspective", { detail: { id: "play" } }));
  if (importedGameId) toast(`Viewing ${importedGameId}`);
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

// Total fixed-row height used by the WinBox onresize max-height clamp
// (keeps the window from growing taller than the board can usefully fill).
const FIXED_FULL = LIVE_PV_H * 2 + LIVE_EVAL_H * 2 + LIVE_CLOCK_H * 2 + LIVE_GAP * 6;
// Below this body height, drop the pv rows (toggled via .lg-compact).
const LIVE_COMPACT_THRESHOLD = 280;

// Shared construction for live + frozen windows. Builds DOM, mounts the
// board, creates the WinBox, wires the result-overlay/replay-button
// machinery, registers in `liveWindows`. Caller adds WS (live) or
// final-state painting (frozen) and assigns a real `wb.onclose` that
// cleans up its own resources after invoking `disposeShared`.
function buildLiveGameBox({ windowKey, gameId, proxyId, label, engineName, token, tournamentId, top, left, right = 0, boardStyle, avoidRect, initialRect, min, flash, variantClass }) {
  const body = document.createElement("div");
  body.className = "wb-livegame lg-measuring";
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
  const defaultClass = gameId
    ? "sturddle-wb sturddle-wb-live sturddle-wb-live-game no-full no-shadow"
    : "sturddle-wb sturddle-wb-live sturddle-wb-live-proxy no-full no-shadow";
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
    right,
    min,
    mount: body,
    class: variantClass ? `${defaultClass} ${variantClass}` : defaultClass,
  });
  wb._watchOpts = { proxyId, gameId, label, engineName };

  const clampToViewport = () => {
    const maxX = Math.max(left, window.innerWidth  - wb.width);
    const maxY = Math.max(top,  window.innerHeight - wb.height);
    const cx = Math.min(Math.max(wb.x, left), maxX);
    const cy = Math.min(Math.max(wb.y, top),  maxY);
    if (cx !== wb.x || cy !== wb.y) wb.move(cx, cy);
  };
  if (!min) clampToViewport();
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
  wb.svBoard = board;
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
        await replayTournamentGame({ tournamentId, gameN: reconciledGameN, token, pairId: gameId });
      } finally {
        replayInFlight = false;
        replayBtnEl.disabled = false;
      }
    });
  }
  if (flash && !min) requestAnimationFrame(() => flashWindow(wb));

  // Compact toggle hides the pv rows when vertical room is tight. The
  // board itself is sized by CSS (flex: 1 1 0 + aspect-ratio: 1 in
  // .lg-board); we only read its measured width to publish to cm-chessboard
  // and to clamp the clock rows below the board. .lg-measuring hides the
  // clocks until the first real measurement lands.
  function constrainAndResize() {
    body.classList.toggle("lg-compact", body.clientHeight < LIVE_COMPACT_THRESHOLD);
    const sz = boardHost.clientWidth;
    if (sz > 0) {
      body.style.setProperty("--lg-board-w", `${sz}px`);
      body.classList.remove("lg-measuring");
    }
    board.forceResize();
  }
  // Observe both: body changes drive compact-mode toggle; boardHost
  // changes catch shrinks of the board's flex slot. Eval/pv rows
  // reserve their populated height in CSS so the board doesn't
  // snap-shrink on the first event.
  const ro = new ResizeObserver(constrainAndResize);
  ro.observe(body);
  ro.observe(boardHost);
  // Initial pass synchronously (body is already attached by WinBox.mount)
  // so --lg-board-w is set before the first paint -- otherwise the clock
  // rows briefly render at body width before the ResizeObserver fires.
  constrainAndResize();

  // Result banner. Late "*" downgrades after a real result are ignored
  // (per-pair WS + game_reconciled can arrive in either order).
  let resultPainted = false;
  function showResult(result, termination) {
    const isReal = result && result !== "*";
    if (resultPainted && !isReal) return;
    if (isReal) resultPainted = true;
    const score = isReal ? result : "game ended";
    const term = (termination && termination !== "unknown") ? terminationPhrase(termination) : "";
    resultScoreEl.textContent = score;
    resultTerminationEl.textContent = term;
    resultOverlayEl.hidden = false;
  }

  function disposeShared() {
    ro.disconnect();
    board.destroy();
    if (gameId) window.removeEventListener("sturddle:reconciled", onReconciled);
    liveWindows.delete(windowKey);
    window.dispatchEvent(new CustomEvent("sturddle:livegame-closed"));
  }

  return {
    wb, body, board, boardHost,
    refs: {
      evalScoreEl, evalDepthEl, evalTbhitsEl, pvEl,
      oppEvalScoreEl, oppEvalDepthEl, oppEvalTbhitsEl, oppPvEl,
      clockTopEl, clockBottomEl,
      topNameEl, bottomNameEl, topTimeEl, bottomTimeEl,
      resultOverlayEl, resultScoreEl, resultTerminationEl, replayBtnEl,
    },
    showResult,
    setReplayGameN(n) {
      if (n != null && tournamentId) {
        reconciledGameN = n;
        replayBtnEl.hidden = false;
      }
    },
    disposeShared,
  };
}

export function openLiveGameWindow({ proxyId, gameId = null, windowKey = gameId ?? proxyId, label, engineName, token, tournamentId = null, top = 0, left = 0, right = 0, boardStyle = null, avoidRect = null, initialRect = null, min = false, flash = true }) {
  if (DEBUG_WATCH) console.log("[WATCH] openLiveGameWindow", { proxyId, gameId, windowKey, label });
  if (!windowKey) {
    console.error("[WATCH] no windowKey -- need at least one of proxyId/gameId", { proxyId, gameId });
    return;
  }
  const existing = liveWindows.get(windowKey);
  if (existing) {
    if (DEBUG_WATCH) console.log("[WATCH] window already open -- focusing", { windowKey });
    if (existing.min) existing.restore();
    existing.focus();
    flashWindow(existing);
    return { wb: existing, alreadyOpen: true };
  }

  const built = buildLiveGameBox({
    windowKey, gameId, proxyId, label, engineName, token, tournamentId,
    top, left, right, boardStyle, avoidRect, initialRect, min, flash,
    variantClass: null,
  });
  const { wb, body, board, refs, showResult, disposeShared } = built;
  const {
    evalScoreEl, evalDepthEl, evalTbhitsEl, pvEl,
    oppEvalScoreEl, oppEvalDepthEl, oppEvalTbhitsEl, oppPvEl,
    clockTopEl, clockBottomEl,
    topNameEl, bottomNameEl, topTimeEl, bottomTimeEl,
  } = refs;

  let ws = null;
  let engineColor = null;
  let opponentName = null;
  let timerInterval = null;
  let activeDeadline = 0;
  let currentFen = null;
  let positionGen = 0;
  let lastWtime = null;
  let lastBtime = null;
  // rAF coalescer for board.setPosition. Bursts of position events
  // (legitimate engine floods, or the post-background drain when a
  // hidden tab regains focus) would otherwise stack animations.
  // Latest pending FEN wins; superseded ones never animate. Background
  // tabs accumulate at most one pending paint (browser pauses rAF).
  let pendingPosition = null;
  let positionRafToken = 0;

  function schedulePositionPaint() {
    if (positionRafToken) return;
    positionRafToken = requestAnimationFrame(() => {
      positionRafToken = 0;
      const p = pendingPosition;
      pendingPosition = null;
      if (!p) return;
      board.setPosition(p.fen, p.lastMove, p.animated);
      board.clearArrows();
    });
  }

  function queuePositionPaint(fen, lastMove) {
    // Skip animation when the tab is hidden -- there is no one to watch
    // it, and on return we want to snap to the final state, not replay
    // the queued cascade.
    pendingPosition = {
      fen,
      lastMove,
      animated: document.visibilityState === "visible",
    };
    schedulePositionPaint();
  }

  let wbClosed = false;
  wb.onclose = () => {
    wbClosed = true;
    if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
    if (positionRafToken) {
      cancelAnimationFrame(positionRafToken);
      positionRafToken = 0;
      pendingPosition = null;
    }
    if (ws) try { ws.close(); } catch { /* */ }
    disposeShared();
    return false;
  };

  // Open WS subscription. Auth carried by cookie.
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const wsTarget = gameId
    ? `game/${encodeURIComponent(gameId)}`
    : `proxy/${encodeURIComponent(proxyId)}`;
  const url = `${proto}//${location.host}/ws/tournament/${wsTarget}`;
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
    const eName = engineName || (color === "white" ? "White" : "Black");
    bottomNameEl.textContent = eName;
    bottomNameEl.title = eName;
    if (!opponentName) {
      const oName = oppColor === "white" ? "White" : "Black";
      topNameEl.textContent = oName;
      topNameEl.title = oName;
    }
    clockBottomEl.dataset.color = color;
    clockTopEl.dataset.color = oppColor;
  }

  function setOpponentName(name) {
    if (!name || name === opponentName) return;
    opponentName = name;
    topNameEl.textContent = name;
    topNameEl.title = name;
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
        queuePositionPaint(newFen, uciMove);
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
          queuePositionPaint(p.fen, p.last_move || null);
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

  function pvArrowMove(p) {
    const pv = p.pv_uci;
    if (!pv || !pv.length) return null;
    if (!p.time || p.time < ARROW_MIN_TIME_MS) return null;
    const m = pv[0];
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

  function fmtScore(p) {
    const score = p.score;
    if (!score) return "--";
    if (score.cp != null) return (score.cp >= 0 ? "+" : "") + (score.cp / 100).toFixed(2);
    if (score.mate != null) return `M${score.mate}`;
    return "--";
  }

  function renderOpponentEval(p) {
    oppEvalScoreEl.textContent = fmtScore(p);
    oppEvalDepthEl.textContent = p.depth != null
      ? (p.seldepth != null ? `d${p.depth}/${p.seldepth}` : `d${p.depth}`)
      : "";
    oppEvalTbhitsEl.textContent = p.tbhits ? `tb ${p.tbhits}` : "";
    const pv = p.pv_uci;
    if (pv && pv.length) oppPvEl.textContent = pv.slice(0, 12).join(" ");
  }

  function renderEval(p) {
    evalScoreEl.textContent = fmtScore(p);
    evalDepthEl.textContent = p.depth != null
      ? (p.seldepth != null ? `d${p.depth}/${p.seldepth}` : `d${p.depth}`)
      : "";
    evalTbhitsEl.textContent = p.tbhits ? `tb ${p.tbhits}` : "";
    const pv = p.pv_uci;
    if (pv && pv.length) {
      pvEl.textContent = pv.slice(0, 12).join(" ");
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


// Frozen rehydration of a finished tournament game. Same window
// look/feel as the live variant but no WebSocket; final position and
// result come from the per-game PGN endpoint. Used when reopening a
// workspace whose snapshot has resolved game entries.
export function openFrozenGameWindow({
  proxyId, gameId, windowKey = gameId, label, engineName,
  token, tournamentId, gameN, result, termination,
  top = 0, left = 0, right = 0, boardStyle = null, initialRect = null, min = false, flash = true,
}) {
  if (!windowKey) {
    console.error("[FROZEN] no windowKey", { proxyId, gameId });
    return;
  }
  const existing = liveWindows.get(windowKey);
  if (existing) {
    if (existing.min) existing.restore();
    existing.focus();
    flashWindow(existing);
    return { wb: existing, alreadyOpen: true };
  }
  if (gameN == null || !tournamentId) {
    console.error("[FROZEN] missing gameN/tournamentId -- cannot rehydrate", { gameN, tournamentId });
    return;
  }

  const built = buildLiveGameBox({
    windowKey, gameId, proxyId, label, engineName, token, tournamentId,
    top, left, right, boardStyle, avoidRect: null, initialRect, min, flash,
    variantClass: "sturddle-wb-live-frozen",
  });
  const { wb, board, refs, showResult, setReplayGameN, disposeShared } = built;
  const { topNameEl, bottomNameEl, clockTopEl, clockBottomEl } = refs;

  wb.onclose = () => {
    disposeShared();
    return false;
  };

  // Paint result immediately so the user sees state even before the
  // PGN fetch returns; refined once headers are in.
  showResult(result, termination);
  setReplayGameN(gameN);

  const headers = { "Content-Type": "application/json" };
  fetch(`/api/tournaments/${encodeURIComponent(tournamentId)}/games/${gameN}/pgn`, { headers })
    .then(async (res) => {
      if (!res.ok) throw new Error(`fetch pgn -> ${res.status}`);
      return res.json();
    })
    .then((rec) => {
      // Identity check, not just .has(): rapid close/reopen of the same
      // windowKey would otherwise paint into a new window via captured
      // refs that point at the old (detached) DOM nodes.
      if (liveWindows.get(windowKey) !== wb) return;
      const fen = rec.final_fen;
      const lastMove = rec.last_move || null;
      // Engine that was watched live is the one whose label matched
      // engineName. Fall back to bottom = engineName side.
      const engineIsWhite = engineName && rec.engine_white === engineName;
      const color = engineIsWhite ? "white" : "black";
      const oppColor = engineIsWhite ? "black" : "white";
      board.setSide(color);
      if (fen) board.setPosition(fen, lastMove);
      board.clearArrows();
      const bName = engineName || (engineIsWhite ? rec.engine_white : rec.engine_black) || "?";
      const tName = engineIsWhite ? rec.engine_black : rec.engine_white;
      bottomNameEl.textContent = bName;
      bottomNameEl.title = bName;
      topNameEl.textContent = tName;
      topNameEl.title = tName || "";
      clockBottomEl.dataset.color = color;
      clockTopEl.dataset.color = oppColor;
      // Refine banner from PGN headers if the snapshot was stale.
      showResult(rec.result, rec.termination);
    })
    .catch((e) => {
      // 404 = game pruned/missing. Close silently per the agreed policy.
      console.warn("[FROZEN] pgn fetch failed; closing", e);
      try { wb.close(); } catch { /* */ }
    });

  return { wb, alreadyOpen: false };
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

