// Live game window: one engine's proxy stream -> board from that engine's POV.
// "Attach to engine, not to game" -- opponent POV needs a second window.
// Renders: board (server-computed FEN from `position`), eval/depth/PV from
// this engine's `info`, clocks from `go wtime/btime`, last bestmove highlight.

import { mountBoard } from "./board.js";
import { confirm, reportError, toast } from "./dialogs.js";
import { isPlayInProgress, isViewing, isAnalyzing, getViewingHash, getViewingSummary } from "./perspectives/play.js";
import { confirmReplaceViewedGame } from "./import-position-dialog.js";
import { APP_EVT } from "./app-events.js";
import { SIDE, FEN_STM } from "./chess-consts.js";
import { createEvalGraph } from "./eval-graph.js";
import { createPvTable } from "./pv-table.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { fmtClock, fmtCount, fmtScore, flashWindow, rafCoalesce } from "./wb-utils.js";
import { terminationPhrase } from "./format-termination.js";

// Eval-row info strings (shared by own-side and opponent rows). Each blanks
// when its field is absent. Format: "d:<depth>/<seldepth>", "nps:<count>",
// "tb:<tbhits>".
function _fmtDepth(p) {
  if (p.depth == null) return "";
  return p.seldepth != null ? `d:${p.depth}/${p.seldepth}` : `d:${p.depth}`;
}
function _fmtNps(p) {
  return p.nps ? `nps:${fmtCount(p.nps)}` : "";
}
// hashfull is per-mille (UCI); shown as percent.
function _fmtHash(p) {
  return p.hashfull ? `h:${Math.round(p.hashfull / 10)}%` : "";
}
function _fmtTb(p) {
  return p.tbhits ? `tb:${p.tbhits}` : "";
}
// Write the depth/nps/hashfull/tbhits stats into one eval row's spans.
function _applyEvalInfo({ depthEl, npsEl, hashEl, tbhitsEl }, p) {
  depthEl.textContent = _fmtDepth(p);
  npsEl.textContent = _fmtNps(p);
  hashEl.textContent = _fmtHash(p);
  tbhitsEl.textContent = _fmtTb(p);
}

// 0-based ply about to be played in `fen` (fullmove + side to move).
function fenPly(fen) {
  const parts = fen.split(" ");
  const fullmove = parseInt(parts[5], 10) || 1;
  return (fullmove - 1) * 2 + (parts[1] === FEN_STM.BLACK ? 1 : 0);
}

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
    reportError(null, "Review: fetch failed", e);
    return;
  }
  if (isPlayInProgress()) {
    const ok = await confirm({
      message: "Discard your in-progress game and review this tournament game?",
      okLabel: "Review",
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
      window.dispatchEvent(new CustomEvent(APP_EVT.ACTIVATE_PERSPECTIVE, {
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
    reportError(null, "Review: import failed", e);
    return;
  }
  // Server-side invariant: when the client supplied a game_id, the
  // import response must echo it back exactly. Mismatch is a unification
  // bug, not a race -- surface it loudly.
  if (pairId && importedGameId && importedGameId !== pairId) {
    reportError(
      null,
      "Review: server returned game_id != pair_id (unification bug)",
      new Error(`stored=${importedGameId} pair=${pairId}`),
    );
    return;
  }
  window.dispatchEvent(new CustomEvent(APP_EVT.ACTIVATE_PERSPECTIVE, { detail: { id: "play" } }));
  if (importedGameId) toast(`Viewing ${importedGameId}`);
}

// Flip to true to re-enable verbose [WATCH] tracing for debugging
// intermittent click-watch failures. Errors are always logged.
export const DEBUG_WATCH = false;

const liveWindows = new Map(); // windowKey -> WinBox instance

const LIVE_MIN_BOARD    = 200; // px -- smallest usable board side
const LIVE_WINBOX_TITLE = 35;  // px -- WinBox title bar

// Row heights and gaps scale with the root font size. CSS rules mirror these:
//   container gap: 0.25em
//   clock: 1rem * 1.2 line-height + 0.625em vertical padding
//   eval:  0.8125rem * 1.4 line-height
//   pv:    0.6875rem * 1.4 line-height + 0.125em bottom padding
function liveFontMetrics() {
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
  return {
    clockH: Math.ceil(rem * 1.2 + rem * 0.625),
    evalH:  Math.ceil(rem * 0.8125 * 1.4),
    pvH:    Math.ceil(rem * 0.6875 * 1.4 + rem * 0.125),
    gap:    Math.ceil(rem * 0.25),
  };
}

const ARROW_MIN_TIME_MS = 250; // skip arrow if side-to-move has less time than this

export const LIVE_MIN_WIDTH = LIVE_MIN_BOARD;
// 7 flex children: pv-top, eval-top, clock-top, board, clock-bottom, eval-bottom, pv-bottom -- 6 gaps.
export function LIVE_MIN_HEIGHT() {
  const { clockH, evalH, pvH, gap } = liveFontMetrics();
  return LIVE_WINBOX_TITLE + pvH * 2 + evalH * 2 + clockH * 2 + LIVE_MIN_BOARD + gap * 6;
}


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

// Total fixed-row height for the onresize max-height clamp.
function liveFixedFull() {
  const { clockH, evalH, pvH, gap } = liveFontMetrics();
  return pvH * 2 + evalH * 2 + clockH * 2 + gap * 6;
}
// Below this body height, drop the pv rows (toggled via .lg-compact).
const LIVE_COMPACT_THRESHOLD = 280;
// Side PV panels appear when each gutter beside the board is at least
// this fraction of the board's width.
const PV_SIDE_MIN_RATIO = 1/3;
// px between the board and each side PV panel.
const PV_SIDE_GAP = 8;

// Shared construction for live + frozen windows. Builds DOM, mounts the
// board, creates the WinBox, wires the result-overlay/replay-button
// machinery, registers in `liveWindows`. Caller adds WS (live) or
// final-state painting (frozen) and assigns a real `wb.onclose` that
// cleans up its own resources after invoking `disposeShared`.
function buildLiveGameBox({ windowKey, gameId, proxyId, label, engineName, token, tournamentId, top, left, right = 0, boardStyle, avoidRect, initialRect, min, max = false, flash, variantClass, root = null, onPvSides = null }) {
  const body = document.createElement("div");
  body.className = "wb-livegame lg-measuring";
  body.innerHTML = `
    <div class="lg-pv lg-pv-top muted"></div>
    <div class="lg-eval lg-eval-top">
      <span class="lg-eval-score lg-eval-score-top"></span>
      <span class="lg-eval-depth lg-eval-depth-top muted"></span>
      <span class="lg-eval-nps lg-eval-nps-top muted"></span>
      <span class="lg-eval-hash lg-eval-hash-top muted"></span>
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
        <button type="button" class="lg-result-replay" hidden>Review</button>
      </div>
    </div>
    <div class="clock-row lg-clock-bottom">
      <span class="clock-name lg-bottom-name">--</span>
      <span class="clock-time lg-bottom-time">--</span>
    </div>
    <div class="lg-eval lg-eval-bottom">
      <span class="lg-eval-score lg-eval-score-bottom"></span>
      <span class="lg-eval-depth lg-eval-depth-bottom muted"></span>
      <span class="lg-eval-nps lg-eval-nps-bottom muted"></span>
      <span class="lg-eval-hash lg-eval-hash-bottom muted"></span>
      <span class="lg-eval-tbhits lg-eval-tbhits-bottom muted"></span>
    </div>
    <div class="lg-pv lg-pv-bottom muted"></div>
    <div class="lg-pv-side lg-pv-side-left">
      <div class="lg-pv-side-name" data-color="black"></div>
      <div class="lg-pv-side-table lg-pv-side-table-black"></div>
      <div class="lg-pv-side-name" data-color="white"></div>
      <div class="lg-pv-side-table lg-pv-side-table-white"></div>
    </div>
    <div class="lg-pv-side lg-pv-side-right">
      <div class="lg-pv-side-name lg-eval-graph-title">Eval</div>
      <div class="lg-eval-graph-host"></div>
    </div>
  `;

  const boardHost = body.querySelector(".lg-board");
  const board = mountBoard({
    element: boardHost,
    styleId: boardStyle,
    onMove: () => {}, // read-only -- moves come from the server.
  });
  // Hide the window body until the board is fully drawn (revealed via wb._ready
  // below) so it appears complete in one shot, not assembling piece by piece.
  body.style.visibility = "hidden";

  const evalScoreEl = body.querySelector(".lg-eval-score-bottom");
  const evalDepthEl = body.querySelector(".lg-eval-depth-bottom");
  const evalNpsEl = body.querySelector(".lg-eval-nps-bottom");
  const evalHashEl = body.querySelector(".lg-eval-hash-bottom");
  const evalTbhitsEl = body.querySelector(".lg-eval-tbhits-bottom");
  const pvEl = body.querySelector(".lg-pv-bottom");
  const oppEvalScoreEl = body.querySelector(".lg-eval-score-top");
  const oppEvalDepthEl = body.querySelector(".lg-eval-depth-top");
  const oppEvalNpsEl = body.querySelector(".lg-eval-nps-top");
  const oppEvalHashEl = body.querySelector(".lg-eval-hash-top");
  const oppEvalTbhitsEl = body.querySelector(".lg-eval-tbhits-top");
  const oppPvEl = body.querySelector(".lg-pv-top");
  const clockTopEl = body.querySelector(".lg-clock-top");
  const clockBottomEl = body.querySelector(".lg-clock-bottom");
  const topNameEl = body.querySelector(".lg-top-name");
  const bottomNameEl = body.querySelector(".lg-bottom-name");
  const topTimeEl = body.querySelector(".lg-top-time");
  const bottomTimeEl = body.querySelector(".lg-bottom-time");
  const pvTableBlackEl = body.querySelector(".lg-pv-side-table-black");
  const pvTableWhiteEl = body.querySelector(".lg-pv-side-table-white");
  const pvNameBlackEl = body.querySelector('.lg-pv-side-name[data-color="black"]');
  const pvNameWhiteEl = body.querySelector('.lg-pv-side-name[data-color="white"]');
  const evalGraphHostEl = body.querySelector(".lg-eval-graph-host");
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
    ? "sturddle-wb sturddle-wb-live sturddle-wb-live-game no-full"
    : "sturddle-wb sturddle-wb-live sturddle-wb-live-proxy no-full";
  const wb = new WinBox({
    title: titleWithTag,
    width: initialWidth,
    ...(initialHeight ? { height: initialHeight } : {}),
    minwidth: LIVE_MIN_WIDTH,
    minheight: LIVE_MIN_HEIGHT(),
    x: initialRect ? initialRect.x : `${20 + (idx * 4)}%`,
    y: initialRect ? initialRect.y : `${5 + (idx * 4)}%`,
    top,
    left,
    right,
    min,
    max,
    mount: body,
    ...(root ? { root } : {}),
    class: variantClass ? `${defaultClass} ${variantClass}` : defaultClass,
  });
  wb._watchOpts = { proxyId, gameId, label, engineName };
  // Reveals this window's body once drawn; surfaced so the opener can gate a
  // perspective-wide reveal until every restored board is ready.
  wb._ready = board.ready.then(() => { body.style.visibility = ""; });

  const clampToViewport = () => {
    const maxX = Math.max(left, window.innerWidth  - wb.width);
    const maxY = Math.max(top,  window.innerHeight - wb.height);
    const cx = Math.min(Math.max(wb.x, left), maxX);
    const cy = Math.min(Math.max(wb.y, top),  maxY);
    if (cx !== wb.x || cy !== wb.y) wb.move(cx, cy);
  };
  // Rooted (Studio) boards are grid-placed inside their region, not the
  // viewport, so the viewport clamp would mis-move them.
  if (!root && !min && !max) clampToViewport();
  const scheduleConstrain = rafCoalesce(constrainAndResize);
  // Clamp height so the window can't grow taller than the board needs:
  // a portrait-stretched window wastes space and looks broken.
  wb.onresize = (w, h) => {
    const maxH = w + LIVE_WINBOX_TITLE + liveFixedFull();
    if (h > maxH) wb.resize(w, maxH);
    // Maximize/restore can land on identical pixel sizes; the
    // ResizeObserver stays silent then, so re-check the wb.max-gated
    // side panels. rAF: WinBox sets wb.max after this callback.
    scheduleConstrain();
  };
  if (avoidRect) avoidOverlap(wb, avoidRect, top, left, idx * 24);
  // WinBox doesn't expose its config minwidth/minheight as instance fields;
  // stash them so the workspace's tile() can clamp.
  wb.svMinWidth = LIVE_MIN_WIDTH;
  wb.svMinHeight = LIVE_MIN_HEIGHT();
  wb.svBoard = board;
  wb._windowKey = windowKey;
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
    window.addEventListener(APP_EVT.RECONCILED, onReconciled);
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
  let pvSidesOn = false;
  function constrainAndResize() {
    body.classList.toggle("lg-compact", body.clientHeight < LIVE_COMPACT_THRESHOLD);
    // Board slot may be taller than wide (portrait window); cap height to
    // width so the square board sits flush against the clock rows.
    boardHost.style.height = "";
    const sz = Math.min(boardHost.clientWidth, boardHost.clientHeight);
    if (sz > 0) {
      boardHost.style.height = `${sz}px`;
      body.style.setProperty("--lg-board-w", `${sz}px`);
      body.classList.remove("lg-measuring");
    }
    if (onPvSides && sz > 0) {
      const sideW = Math.floor((body.clientWidth - sz) / 2);
      const show = !!wb.max && sideW >= sz * PV_SIDE_MIN_RATIO;
      if (show) {
        // Span from the top clock row to the bottom one (not just the
        // board) -- the gutters are empty there too.
        const sideTop = clockTopEl.offsetTop;
        const sideH = clockBottomEl.offsetTop + clockBottomEl.offsetHeight - sideTop;
        body.style.setProperty("--lg-side-w", `${sideW - PV_SIDE_GAP}px`);
        body.style.setProperty("--lg-side-top", `${sideTop}px`);
        body.style.setProperty("--lg-side-h", `${sideH}px`);
      }
      if (show !== pvSidesOn) {
        pvSidesOn = show;
        body.classList.toggle("lg-pv-sides", show);
        onPvSides(show);
      }
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
    scheduleConstrain.cancel();
    ro.disconnect();
    board.destroy();
    if (gameId) window.removeEventListener(APP_EVT.RECONCILED, onReconciled);
    liveWindows.delete(windowKey);
    window.dispatchEvent(new CustomEvent(APP_EVT.LIVEGAME_CLOSED));
  }

  return {
    wb, body, board, boardHost,
    refs: {
      evalScoreEl, evalDepthEl, evalNpsEl, evalHashEl, evalTbhitsEl, pvEl,
      oppEvalScoreEl, oppEvalDepthEl, oppEvalNpsEl, oppEvalHashEl, oppEvalTbhitsEl, oppPvEl,
      clockTopEl, clockBottomEl,
      topNameEl, bottomNameEl, topTimeEl, bottomTimeEl,
      resultOverlayEl, resultScoreEl, resultTerminationEl, replayBtnEl,
      pvTableBlackEl, pvTableWhiteEl, pvNameBlackEl, pvNameWhiteEl, evalGraphHostEl,
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

// oversized-ok: stateful live-game controller -- a WebSocket feed, clock
// timers, and board paint all coordinate over shared state (ws, engineColor,
// currentFen, positionGen, clock fields). Closures return a control API;
// splitting would scatter the feed/clock/paint coordination.
export function openLiveGameWindow({ proxyId, gameId = null, windowKey = gameId ?? proxyId, label, engineName, token, tournamentId = null, top = 0, left = 0, right = 0, boardStyle = null, avoidRect = null, initialRect = null, min = false, max = false, flash = true, root = null, variantClass = null }) {
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

  // Gutter panels: left stacks the PV tables (black over white, feeds
  // re-routed when the engine's color changes); right is the eval graph.
  // Table updates are skipped while hidden (not maximized / narrow) --
  // zero per-info cost; graph samples accumulate regardless (cheap).
  const pvSideWhite = createPvTable({ colWidthsKey: STORAGE_KEY.LIVE_PVTABLE_COL_WIDTHS });
  const pvSideBlack = createPvTable({ colWidthsKey: STORAGE_KEY.LIVE_PVTABLE_COL_WIDTHS });
  const evalGraph = createEvalGraph();
  let pvSidesVisible = false;

  const built = buildLiveGameBox({
    windowKey, gameId, proxyId, label, engineName, token, tournamentId,
    top, left, right, boardStyle, avoidRect, initialRect, min, max, flash,
    variantClass, root,
    onPvSides: (visible) => {
      pvSidesVisible = visible;
      evalGraph.setVisible(visible);
      // Re-run the width fit on reveal: rows updated while hidden
      // (display:none) measured a scrollWidth of 0.
      if (visible) { pvSideWhite.fit(); pvSideBlack.fit(); }
    },
  });
  const { wb, body, board, refs, showResult, disposeShared } = built;
  refs.pvTableWhiteEl.appendChild(pvSideWhite.el);
  refs.pvTableBlackEl.appendChild(pvSideBlack.el);
  refs.evalGraphHostEl.appendChild(evalGraph.el);
  const {
    evalScoreEl, evalDepthEl, evalNpsEl, evalHashEl, evalTbhitsEl, pvEl,
    oppEvalScoreEl, oppEvalDepthEl, oppEvalNpsEl, oppEvalHashEl, oppEvalTbhitsEl, oppPvEl,
    clockTopEl, clockBottomEl,
    topNameEl, bottomNameEl, topTimeEl, bottomTimeEl,
    pvNameBlackEl, pvNameWhiteEl,
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
  let shownPly = null; // ply currently painted on the board (null until first)
  const schedulePositionPaint = rafCoalesce(() => {
    const p = pendingPosition;
    pendingPosition = null;
    // wbClosed: a producer that resumed after close (e.g. applyBestMove's
    // fetch) must not repaint a destroyed board.
    if (!p || wbClosed) return;
    // Animate only a single forward ply (a move being watched); first paint
    // and multi-ply backlog jumps snap so pieces don't glide catching up.
    const toPly = fenPly(p.fen);
    const animated = p.animated && shownPly !== null && toPly - shownPly === 1;
    shownPly = toPly;
    board.setPosition(p.fen, p.lastMove, animated);
    board.clearArrows();
  });

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
    schedulePositionPaint.cancel();
    pendingPosition = null;
    pvSideWhite.dispose();
    pvSideBlack.dispose();
    evalGraph.dispose();
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

  // Route own/opponent info feeds to the color-fixed panels. Before the
  // first position lands (engineColor null) own defaults to white.
  function pvOwn() { return engineColor === SIDE.BLACK ? pvSideBlack : pvSideWhite; }
  function pvOpp() { return engineColor === SIDE.BLACK ? pvSideWhite : pvSideBlack; }

  // Eval-graph sampling: each feed's latest info is banked as that
  // side's final eval at the move boundary (own: bestmove; opponent:
  // the next own-position, which implies their move completed).
  let lastOwnInfo = null;
  let lastOppInfo = null;
  let pendingOwnPly = null;
  let lastSampledPly = -1;

  function addEvalSample(ply, info, mover) {
    if (ply == null || ply < 0 || !info?.score) return;
    // Ply moving backwards = a new game on a reused proxy window.
    if (ply < lastSampledPly) { lastSampledPly = -1; evalGraph.clear(); }
    if (evalGraph.add(ply, info.score, mover === SIDE.WHITE) && ply > lastSampledPly) {
      lastSampledPly = ply;
    }
  }

  function setEngineColor(color) {
    engineColor = color;
    const oppColor = color === SIDE.WHITE ? SIDE.BLACK : SIDE.WHITE;
    const eName = engineName || (color === SIDE.WHITE ? "White" : "Black");
    bottomNameEl.textContent = eName;
    bottomNameEl.title = eName;
    if (!opponentName) {
      const oName = oppColor === SIDE.WHITE ? "White" : "Black";
      topNameEl.textContent = oName;
      topNameEl.title = oName;
    }
    clockBottomEl.dataset.color = color;
    clockTopEl.dataset.color = oppColor;
    syncPvSideNames();
  }

  // Panel headers are color-fixed (black over white); write each
  // player's name into the header matching their color.
  function syncPvSideNames() {
    if (!engineColor) return;
    const oppColor = engineColor === SIDE.WHITE ? SIDE.BLACK : SIDE.WHITE;
    const eName = engineName || (engineColor === SIDE.WHITE ? "White" : "Black");
    const oName = opponentName || (oppColor === SIDE.WHITE ? "White" : "Black");
    pvNameWhiteEl.textContent = engineColor === SIDE.WHITE ? eName : oName;
    pvNameBlackEl.textContent = engineColor === SIDE.WHITE ? oName : eName;
  }

  function setOpponentName(name) {
    if (!name || name === opponentName) return;
    opponentName = name;
    topNameEl.textContent = name;
    topNameEl.title = name;
    syncPvSideNames();
  }

  function updateClocks(wtime, btime) {
    if (!engineColor || wtime == null || btime == null) return;
    bottomTimeEl.textContent = fmtClock((engineColor === SIDE.WHITE ? wtime : btime) / 1000, { tenthsBelow: 60 });
    topTimeEl.textContent = fmtClock((engineColor === SIDE.WHITE ? btime : wtime) / 1000, { tenthsBelow: 60 });
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
          const color = turn === FEN_STM.BLACK ? SIDE.BLACK : SIDE.WHITE;
          if (color !== engineColor) {
            setEngineColor(color);
            board.setSide(color);
          }
          const ply = fenPly(p.fen);
          // Opponent just completed ply-1; bank their final eval.
          if (lastOppInfo) {
            addEvalSample(ply - 1, lastOppInfo, color === SIDE.WHITE ? SIDE.BLACK : SIDE.WHITE);
            lastOppInfo = null;
          }
          pendingOwnPly = ply;
          queuePositionPaint(p.fen, p.last_move || null);
        }
        break;
      case "info":
        lastOwnInfo = p;
        renderEval(p);
        break;
      case "go":
        // Own engine starts a new search; drop the previous one's lines.
        pvOwn().clear();
        lastWtime = p.wtime ?? lastWtime;
        lastBtime = p.btime ?? lastBtime;
        updateClocks(p.wtime, p.btime);
        clockTopEl.classList.remove("active");
        clockBottomEl.classList.toggle("active", !!engineColor);
        if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
        if (engineColor && p.wtime != null && p.btime != null) {
          const startMs = engineColor === SIDE.WHITE ? p.wtime : p.btime;
          activeDeadline = Date.now() + startMs;
          const tick = () => {
            const remaining = Math.max(0, activeDeadline - Date.now());
            bottomTimeEl.textContent = fmtClock(remaining / 1000, { tenthsBelow: 60 });
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
        // Opponent thinks next; its panel restarts (heuristic depth-reset
        // in createPvTable backstops ponder/missed transitions).
        pvOpp().clear();
        // Own move done at pendingOwnPly: bank our final eval.
        if (lastOwnInfo) {
          addEvalSample(pendingOwnPly, lastOwnInfo, engineColor);
          lastOwnInfo = null;
        }
        if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
        clockBottomEl.classList.remove("active");
        clockTopEl.classList.toggle("active", !!engineColor);
        if (currentFen && p.move) applyBestMove(p.move);
        if (engineColor && lastWtime != null && lastBtime != null) {
          const oppMs = engineColor === SIDE.WHITE ? lastBtime : lastWtime;
          activeDeadline = Date.now() + oppMs;
          const tick = () => {
            const remaining = Math.max(0, activeDeadline - Date.now());
            topTimeEl.textContent = fmtClock(remaining / 1000, { tenthsBelow: 60 });
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
    lastOppInfo = p;
    renderOpponentEval(p);
    if (pvSidesVisible) pvOpp().update(p, p.pv_uci?.join(" "));
    const m = pvArrowMove(p);
    if (m) board.setOpponentArrow(m.slice(0, 2), m.slice(2, 4));
  }

  function renderOpponentEval(p) {
    oppEvalScoreEl.textContent = fmtScore(p.score, { empty: "--", matePrefix: "M", signed: true });
    _applyEvalInfo({ depthEl: oppEvalDepthEl, npsEl: oppEvalNpsEl, hashEl: oppEvalHashEl, tbhitsEl: oppEvalTbhitsEl }, p);
    const pv = p.pv_uci;
    if (pv && pv.length) oppPvEl.textContent = pv.slice(0, 12).join(" ");
  }

  function renderEval(p) {
    evalScoreEl.textContent = fmtScore(p.score, { empty: "--", matePrefix: "M", signed: true });
    _applyEvalInfo({ depthEl: evalDepthEl, npsEl: evalNpsEl, hashEl: evalHashEl, tbhitsEl: evalTbhitsEl }, p);
    if (pvSidesVisible) pvOwn().update(p, p.pv_uci?.join(" "));
    const pv = p.pv_uci;
    if (pv && pv.length) {
      pvEl.textContent = pv.slice(0, 12).join(" ");
      const m = pvArrowMove(p);
      if (m) board.setArrow(m.slice(0, 2), m.slice(2, 4));
    }
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
  top = 0, left = 0, right = 0, boardStyle = null, initialRect = null, min = false, max = false, flash = true,
  root = null, variantClass = null,
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
    top, left, right, boardStyle, avoidRect: null, initialRect, min, max, flash,
    variantClass: variantClass ? `sturddle-wb-live-frozen ${variantClass}` : "sturddle-wb-live-frozen",
    root,
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
      const color = engineIsWhite ? SIDE.WHITE : SIDE.BLACK;
      const oppColor = engineIsWhite ? SIDE.BLACK : SIDE.WHITE;
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

// Move a window to the end of the open-order Map so it counts as
// most-recently-used (LRU eviction reads insertion order).
export function touchLiveWindow(wb) {
  const key = wb._windowKey;
  if (liveWindows.get(key) !== wb) return;
  liveWindows.delete(key);
  liveWindows.set(key, wb);
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

