// Play perspective: human vs engine.
// Composes the reusable GameView with a game-control bar (New / Take back /
// Resign).

import { mountGameView } from "../game-view.js";
import { APP_EVT } from "../app-events.js";
import { KIND, AI_KIND_PREFIX } from "../game-events.js";
import { SIDE, FEN_STM, RESULT } from "../chess-consts.js";
import { STORAGE_KEY } from "../storage-keys.js";
import { alert as showAlert, confirm, DETAILS_DIALOG_WIDTH, DETAILS_ICON, makeToastDismissBtn, openSettings, reportAiError, reportError, SETTINGS_TAB_ENGINES, stickyToast, toast } from "../dialogs.js";
import { showImportPositionDialog, confirmReplaceViewedGame, confirmDiscardViewedGame } from "../import-position-dialog.js";
import { toggleUciLogWindow, togglePvTableWindow, closeDebugWindows, closeAnalysisOpenedWindows, restoreDebugWindows, snapshotViewAnalysisState, restoreViewAnalysisWindows, setDockContainer, setRailDockContainer, setEvalBarCallbacks, setEvalGraphEnabled, evalBar, getEvalBarApi, setUciLogEngine, clearUciLog, isMobileLayout } from "../play-dock-windows.js";
import {
  commentaryWindow,
  openCommentary,
  closeCommentary,
  setCommentaryText,
  setCommentaryNavHandlers,
  setCommentaryNavState,
  isCommentaryOpen,
} from "../play-commentary-window.js";
import {
  openAi,
  closeAi,
  resetAi,
  appendAiDelta,
  appendAiThinking,
  freezeAiThinking,
  appendAiToolCall,
  markAiToolCallFailed,
  setAiToolCallResult,
  noteAiPosition,
  markAiDone,
  setAiUsage,
  setAiStatus,
  setAiTitle,
  setOnUserCloseAi,
  setOnReanalyzeAi,
  setAiInlineHost,
  isAiOpen,
} from "../play-ai-window.js";
import { terminationLabel } from "../format-termination.js";
import { editAnnotation } from "../annotation-dialog.js";

// Tool name the AI uses to inspect hypothetical positions; the live
// board mirrors `input.fen` while a call with this name is in flight.
const ANALYZE_TOOL_NAME = "analyze";

// Module-scope mirror of "user has a live human-vs-engine game running",
// persisting across perspective remounts. Updated from the perspective's
// board_update / game_result handlers below. Cross-module consumers
// (tournament replay) must NOT read a client mirror -- it is unseeded on a
// fresh page load; they ask the server via GET /game/status instead.
let _playInProgress = false;

// SHA-256 hash and summary of the game currently in view (null when in
// play mode). Lets the import path skip confirmation when the incoming
// game is already loaded.
let _viewingHash = null;
let _viewingSummary = null;

// Last board_update seen by this perspective. Survives unmount so the
// next mount can render the cached state synchronously and resolve
// view.ready before /sync round-trips. The /sync response then
// overrides if anything changed server-side.
let _cachedBoardUpdate = null;

// Difficulty-unavailable sticky toast currently showing. Module scope:
// the toast outlives a perspective remount, so a per-mount flag would
// let the next degraded move stack a duplicate.
let _difficultyToastUp = false;

// AI-error toast handle from the last finished round. Module scope: the
// toast outlives a perspective remount (aiShared is rebuilt fresh per
// mount), so a per-mount field would lose the handle and orphan the toast.
let _dismissAiErrorToast = null;

// Details popup body for the difficulty-unavailable toast: prose with
// the UCI terms as code chips and "choose an engine" deep-linking to
// Settings > Engines (resolving the popup first -- one modal at a time).
function buildDifficultyDetails(engineName, resolve) {
  const code = (term) => {
    const el = document.createElement("code");
    el.textContent = term;
    return el;
  };
  const link = document.createElement("a");
  link.href = "#";
  link.textContent = MSG.DIFFICULTY_DETAILS_LINK;
  link.addEventListener("click", (ev) => {
    ev.preventDefault();
    resolve();
    openSettings(SETTINGS_TAB_ENGINES);
  });
  const frag = document.createDocumentFragment();
  frag.append(
    MSG.DIFFICULTY_DETAILS_MECHANISM_PRE,
    code(MSG.DIFFICULTY_DETAILS_TERM_FULL),
    MSG.DIFFICULTY_DETAILS_MECHANISM_POST,
    `${engineName} `,
    MSG.DIFFICULTY_DETAILS_REST,
    link,
    MSG.DIFFICULTY_DETAILS_REST_HONORS,
    code(MSG.DIFFICULTY_DETAILS_TERM),
    ".",
  );
  return frag;
}

// X-game toast don't-nag flags, persisted across perspective mounts.
// Keyed by game_id. Reset only on hard reload (fresh page load).
const _xgameDismissed = new Map();
function _getXgameDismissed(gameId) {
  return _xgameDismissed.get(gameId) || { parent: false, children: false };
}
function _setXgameDismissed(gameId, key, value) {
  if (!gameId) return;
  const cur = _getXgameDismissed(gameId);
  cur[key] = value;
  _xgameDismissed.set(gameId, cur);
}

// Reduce a game_result payload to the canonical chess result string for
// the header badge. resign/timeout don't carry "1-0"/"0-1" in the payload
// so we derive it from who lost (only human can resign today).
function resultBadge(result) {
  return result === RESULT.DRAW ? "½-½" : result;
}

// Cap server-supplied error detail (engine path / exception text) in toasts.
const MAX_TOAST_DETAIL = 200;

// Settings backing a dock window's visibility: read on refresh, cleared by
// that window's X.
const SETTING_SHOW_PGN_COMMENTS = "view_show_pgn_comments";
const SETTING_SHOW_EVAL_GRAPH = "play_show_eval_graph";

// X on a settings-backed dock window is the same gesture as flipping its
// Display-tab switch off.
function putSettingOff(ctx, key) {
  ctx.api("PUT", "/settings", { [key]: false })
    .catch((e) => reportError(ctx, MSG.SETTING_SAVE_FAILED, e));
}

function humanToMove(state) {
  return state.humanWhite ? state.turn === SIDE.WHITE : state.turn === SIDE.BLACK;
}

// eval_history entries are white POV {cp|mate}; flip for a black engine.
function evalToEnginePov(ev, engineWhite) {
  if (engineWhite) return ev;
  if (ev.mate != null) return { mate: -ev.mate };
  if (ev.cp != null) return { cp: -ev.cp };
  return ev;
}

// Rebuild the eval strip from the authoritative per-ply history: engine
// plies only (human slots are null), in engine POV. Null history (view
// mode / no game) clears it.
function feedEvalBar(state, evalHistory) {
  const bar = getEvalBarApi();
  if (!bar) return;
  const engineWhite = !state.humanWhite;
  // Carry each entry's ply (0-based move index) so a bar click can navigate
  // there; human plies are null and produce no bar.
  const items = [];
  if (Array.isArray(evalHistory)) {
    evalHistory.forEach((ev, ply) => {
      if (ev != null) items.push({ score: evalToEnginePov(ev, engineWhite), ply });
    });
  }
  bar.setSamples(items, engineWhite);
}

function formatResult(payload, humanWhite) {
  const { result, by, loser } = payload;
  if (result === RESULT.WHITE_WIN || result === RESULT.BLACK_WIN) return result;
  if (result === RESULT.DRAW) return resultBadge(result);
  if (result === "resign") {
    const humanLost = by === "human";
    const whiteWins = humanLost ? !humanWhite : humanWhite;
    return whiteWins ? RESULT.WHITE_WIN : RESULT.BLACK_WIN;
  }
  if (result === "timeout") {
    return loser === SIDE.WHITE ? RESULT.BLACK_WIN : RESULT.WHITE_WIN;
  }
  return "";
}

function formatViewGameOver({ result, termination }) {
  const reason = terminationLabel(termination);
  if (result === RESULT.WHITE_WIN) return `${reason} -- White wins.`;
  if (result === RESULT.BLACK_WIN) return `${reason} -- Black wins.`;
  if (result === RESULT.DRAW) return `${reason} -- Draw.`;
  return reason;
}

function formatGameOver(payload, humanWhite) {
  const { result, termination, by, loser } = payload;
  if (result === "resign") {
    return by === "human" ? MSG.YOU_RESIGNED : MSG.ENGINE_RESIGNED;
  }
  if (result === "timeout") {
    const humanLost = (loser === SIDE.WHITE) === humanWhite;
    return humanLost ? MSG.YOU_LOST_ON_TIME : MSG.ENGINE_LOST_ON_TIME;
  }
  const reason = terminationLabel(termination);
  if (result === RESULT.WHITE_WIN || result === RESULT.BLACK_WIN) {
    const humanWon = (result === RESULT.WHITE_WIN) === humanWhite;
    return `${reason} -- ${humanWon ? "you win" : "engine wins"}.`;
  }
  return `${reason} -- Draw.`;
}

const ANALYZE_ICON_STOP = "magnifying-glass-minus";
const ANALYZE_ICON_START = "magnifying-glass-plus";

// Board-terminal endings, mirroring the server's start_analysis guard
// (python-chess is_game_over: no claimable draws, no variant_* -- standard
// boards can't reach them). Pinned by test_view_analyze_forced_set.
const FORCED_TERMINATIONS = new Set([
  "checkmate",
  "stalemate",
  "insufficient_material",
  "seventyfive_moves",
  "fivefold_repetition",
]);

// User-facing copy, grouped for an eventual move to a shared i18n
// catalog. HTML-template aria-labels stay inline (static markup).
const MSG = {
  // Error-report titles + failure toasts.
  MOVE_REJECTED: "Move rejected",
  SETTING_SAVE_FAILED: "Failed to save setting",
  OPEN_GAME_FAILED: "Open game failed",
  NEW_GAME_FAILED: "New game failed",
  RESIGN_FAILED: "Resign failed",
  SAVE_PGN_FAILED: "Save PGN failed",
  TAKEBACK_FAILED: "Take-back failed",
  IMPORT_FAILED: "Import failed",
  SWITCH_SIDES_FAILED: "Switch sides failed",
  RESUME_FAILED: "Resume failed",
  PAUSE_FAILED: "Pause failed",
  EDIT_POSITION_FAILED: "Edit position failed",
  INVALID_POSITION: "Invalid position",
  CANCEL_EDIT_FAILED: "Cancel edit failed",
  NAV_FAILED: "Navigation failed",
  PLAY_FROM_HERE_FAILED: "Play from here failed",
  STOP_ANALYSIS_FAILED: "Stop analysis failed",
  START_ANALYSIS_FAILED: "Start analysis failed",
  REANALYZE_FAILED: "Re-analyze failed",
  ENGINE_CRASHED: "Engine crashed unexpectedly.",
  ANALYSIS_ENGINE_FAILED: "Analysis engine failed to start.",
  DIFFICULTY_UNAVAILABLE: "Difficulty unavailable; playing at full strength.",
  // Details popup fragments, assembled by buildDifficultyDetails():
  // mechanism (UCI term as a code chip), "<engine name> <rest>", then a
  // "choose an engine" deep link to Settings > Engines.
  DIFFICULTY_DETAILS_MECHANISM_PRE:
    "Difficulty works by restricting which root moves the engine may " +
    "search (UCI ",
  DIFFICULTY_DETAILS_TERM_FULL: "go searchmoves",
  DIFFICULTY_DETAILS_MECHANISM_POST: "). ",
  DIFFICULTY_DETAILS_REST:
    "ignores that restriction, so reduced difficulty cannot be " +
    "enforced and games play at full strength. To play at reduced " +
    "difficulty, ",
  DIFFICULTY_DETAILS_LINK: "choose an engine",
  DIFFICULTY_DETAILS_REST_HONORS: " that honors ",
  DIFFICULTY_DETAILS_TERM: "searchmoves",
  DIFFICULTY_GENERIC_ENGINE: "This engine",
  DIFFICULTY_DETAILS_ARIA: "Difficulty details",
  // Confirm dialogs.
  CONFIRM_NEW_GAME: "Cancel the game in progress and start a new one?",
  CONFIRM_RESIGN: "Resign the current game?",
  CONFIRM_IMPORT: "Cancel the current game and import another?",
  CONFIRM_EDIT_FROM_PLAY: "Cancel the game in progress and edit the position?",
  CONFIRM_EDIT_STOP_ANALYSIS: "Stop analysis and edit the position?",
  CONFIRM_MOVE_STOP_ANALYSIS: "Stop analysis and play this move?",
  CONFIRM_LEAVE_EDIT: "Leaving will cancel your position edit. Continue?",
  KEEP_PLAYING: "Keep playing",
  KEEP_ANALYZING: "Keep analyzing",
  PLAY_MOVE: "Play move",
  NEW_GAME: "New game",
  EDIT_POSITION: "Edit position",
  RESIGN: "Resign",
  IMPORT: "Import",
  LEAVE: "Leave",
  STAY: "Stay",
  // Game-over alerts.
  YOU_RESIGNED: "You resigned.",
  ENGINE_RESIGNED: "Engine resigned.",
  YOU_LOST_ON_TIME: "You lost on time.",
  ENGINE_LOST_ON_TIME: "Engine lost on time.",
  // Labels / toasts.
  ANALYSIS_MODE: "Analysis mode",
  STOP_ANALYSIS: "Stop analysis",
  FORKED_FROM: "Forked from ",
  SHOW_VARIATIONS: "Show variations",
  ANALYZE_NEEDS_ENGINE: "Register an engine in Settings to analyze",
  SEARCH_LINES: "Search lines",
  UCI_LOG: "UCI log",
  WHITE_TO_MOVE: "White to move",
  BLACK_TO_MOVE: "Black to move",
};
// Body class set while analysis is on; CSS greys + inert-ifies x-game
// nav links so the user can't jump games mid-analysis.
const XGAME_LOCK_CLASS = "xgame-nav-locked";

// One-shot CSS animation class: pulses the Paused badge + Resume button when
// the user clicks the inert paused board, hinting how to resume.
const PAUSE_HINT_PULSE_CLASS = "pause-hint-pulse";

// Edit-mode popover (side-to-move / castling) placement when the ribbon
// floats: the WinBox body clips overflow, so the popover is portaled to
// <body> and positioned (position: fixed) against the viewport.
const POPOVER_FLOATING_CLASS = "popover-floating";
const POPOVER_GAP = 8; // px between trigger button and floated popover

// "White vs Black (result)" label for an x-game summary.
function formatGameLabel(summary) {
  const s = summary || {};
  const white = s.white || "?";
  const black = s.black || "?";
  const result = s.result && s.result !== "*" ? ` (${s.result})` : "";
  return `${white} vs ${black}${result}`;
}

// Parse a FEN's side-to-move letter and {wK,wQ,bK,bQ} castling-rights map.
function _seedFromFen(fen) {
  const parts = (fen || "").split(" ");
  const stm = parts[1] === FEN_STM.BLACK ? FEN_STM.BLACK : FEN_STM.WHITE;
  const rights = parts[2] || "";
  return {
    stm,
    castling: {
      wK: rights.includes("K"),
      wQ: rights.includes("Q"),
      bK: rights.includes("k"),
      bQ: rights.includes("q"),
    },
  };
}

// Toast button with an icon + accessible label.
function makeToastIconBtn(iconName, label, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "toast-icon-btn";
  btn.setAttribute("aria-label", label);
  btn.setAttribute("title", label);
  const ic = document.createElement("wa-icon");
  ic.setAttribute("name", iconName);
  btn.appendChild(ic);
  btn.addEventListener("click", onClick);
  return btn;
}

function setDisabled(btn, disabled) {
  if (disabled) btn.setAttribute("disabled", "");
  else btn.removeAttribute("disabled");
}

function configureBtn(btn, {
  disabled,
  active,
  label,
  icon,
}) {
  if (disabled !== undefined) setDisabled(btn, disabled);
  if (active !== undefined) btn.classList.toggle("is-active", active);
  if (label !== undefined) {
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
  }
  if (icon !== undefined) {
    btn.querySelector("wa-icon").setAttribute("name", icon);
  }
}

// Apply one AI analysis event to the panel/board. `aiCtx` holds view, api,
// refreshButtons, aiShared. Returns true if the event was an ai_* kind.
function dispatchAiEvent(aiCtx, evt) {
  const { view, refreshButtons, aiShared } = aiCtx;
  switch (evt.kind) {
    case KIND.AI_INFO: {
      const p = evt.payload || {};
      if (typeof p.delta === "string") appendAiDelta(p.delta, p.round ?? 0, p.thinking_ms ?? null);
      if (p.done) {
        // Defensive: tool-call lifecycle can drop the restore signal
        // (cancelled mid-call, round cap, etc.). Always snap back.
        view.restorePosition({ animate: false });
        markAiDone({
          cancelled: !!p.cancelled,
          error: p.error || null,
          errorDetail: p.error_detail || null,
          roundCap: !!p.round_cap,
          verifierRoundCap: !!p.verifier_round_cap,
          noResponse: !!p.no_response,
          noRecommendation: !!p.no_recommendation,
          usage: p.usage || null,
          provider: p.provider || null,
        });
        if (p.error) {
          // Provider errors can be many lines with URLs; the toast shows the
          // first sentence with a Details affordance for the rest. Sticky so
          // a quota/outage failure stays until the user reads it. Failures we
          // recognize by class name also carry the action that fixes them.
          _dismissAiErrorToast = reportAiError(p.error, p.error_detail);
        }
        // End the AI turn on completion AND error (clears the pulse + toast).
        // Cancel is excluded: it self-resolves via stopAnalysisFromUi ->
        // analyzing=false. Without this, an error left the button pulsing.
        if (!p.cancelled) {
          aiShared.turnFinished = true;
          dismissAnalysisToast(aiShared);
          refreshButtons();
        }
        // A failed turn never produced analysis: close the panel locally.
        // The server exits ANALYZING on its own (its analyzing=false
        // board_update clears the spinner) -- we don't POST /stop back.
        if (p.error) teardownAiPanel(aiShared);
      }
      return true;
    }
    case KIND.AI_THINKING: {
      const p = evt.payload || {};
      if (typeof p.delta === "string") appendAiThinking(p.delta, p.round ?? 0);
      else if (Number.isFinite(p.thinking_ms)) freezeAiThinking(p.round ?? 0, p.thinking_ms);
      return true;
    }
    case KIND.AI_TOOL_CALL: {
      const p = evt.payload || {};
      appendAiToolCall({
        round: p.round ?? 0,
        name: p.name,
        input: p.input,
        toolUseId: p.tool_use_id,
        parentToolUseId: p.parent_tool_use_id,
        thinkingMs: p.thinking_ms ?? null,
      });
      // When the model inspects a hypothetical position, mirror
      // the analyzed FEN on the board so the user can follow the
      // AI's reasoning. Restored on ai_tool_call_complete.
      if (p.name === ANALYZE_TOOL_NAME && p.input && typeof p.input.fen === "string") {
        // No animation: tool calls fire faster than the cm-chessboard
        // queue drains while the perspective is hidden (rAF throttled
        // off-screen), producing a "fast replay" on return.
        view.previewPosition(p.input.fen, { animate: false });
      }
      return true;
    }
    case KIND.AI_TOOL_CALL_FAILED: {
      const p = evt.payload || {};
      markAiToolCallFailed({
        toolUseId: p.tool_use_id,
        error: p.error,
        detail: p.detail,
      });
      // Restore in case the failed call was an analyze preview.
      view.restorePosition({ animate: false });
      return true;
    }
    case KIND.AI_TOOL_CALL_COMPLETE: {
      const p = evt.payload || {};
      setAiToolCallResult({ toolUseId: p.tool_use_id, output: p.output });
      if (p.name === ANALYZE_TOOL_NAME) view.restorePosition({ animate: false });
      view.clearArrows();
      view.clearEngineInfo();
      return true;
    }
    case KIND.AI_POSITION_NOTE: {
      const p = evt.payload || {};
      noteAiPosition({ round: p.round ?? 0, surfaces: p.surfaces || [] });
      return true;
    }
    case KIND.AI_USAGE: {
      setAiUsage(evt.payload || null);
      return true;
    }
    case KIND.AI_RECOMMENDATION: {
      // GameView owns the arrow (its own applyEvent draws it live); route
      // through it so replay redraws identically. Stash the event + FEN so
      // the resync board_update on remount, which clears arrows, can
      // re-apply it (same-FEN guard in handleBusEvent).
      view.applyEvent(evt);
      aiShared.recommendation = { evt, fen: view.getFen() };
      return true;
    }
  }
  return false;
}

// Dedupe by seq. Server resets seq to 1 at the start of each turn, so seq=1
// unconditionally marks a new turn and resets the high-water mark. Otherwise a
// single-event turn (e.g. instant error) following a prior turn whose maxSeq is
// also 1 would be swallowed.
function dispatchAiEventOrdered(ai, aiCtx, evt) {
  const seq = evt?.payload?.seq ?? 0;
  // seq=1 marks a new turn: drop the prior turn's stashed recommendation so
  // a fresh analysis can't resurrect a stale arrow before its own lands.
  if (seq === 1) { ai.maxSeq = 0; aiCtx.aiShared.recommendation = null; }
  else if (seq && seq <= ai.maxSeq) return;
  if (seq) ai.maxSeq = seq;
  dispatchAiEvent(aiCtx, evt);
}

// Buffer live AI events while the replay GET is in flight, then drain in seq
// order with dedupe. Avoids the GET-then-subscribe race: live events that fire
// between subscribe and replay arrival are held instead of dispatched
// out-of-order. `adopt` opens the panel even on an empty replay -- the turn
// just started elsewhere and streams in live.
async function rehydrateAiPanel(ai, aiCtx, { adopt = false } = {}) {
  let replayedThrough = 0;
  try {
    const r = await aiCtx.api("GET", "/game/analysis/replay");
    const events = Array.isArray(r?.events) ? r.events : [];
    if (adopt || events.length > 0) {
      openAi();
      resetAi();
      for (const evt of events) dispatchAiEventOrdered(ai, aiCtx, evt);
      replayedThrough = ai.maxSeq;
    }
  } catch { /* */ } finally {
    ai.rehydrating = false;
    const buffered = ai.liveBuffer;
    ai.liveBuffer = [];
    for (const evt of buffered) {
      // Already covered by the replay. The dispatcher's own seq dedupe can't
      // catch a buffered seq=1: it reads as a new turn and resets the mark,
      // re-playing everything after it.
      const seq = evt.payload?.seq ?? 0;
      if (seq > 0 && seq <= replayedThrough) continue;
      dispatchAiEventOrdered(ai, aiCtx, evt);
    }
  }
}

// A streaming ai_* event with the panel closed means another client started
// analysis. A terminal event, or a stop we are driving ourselves, must not
// resurrect the panel -- those dispatch normally (a done payload carries the
// ribbon latch, which is live whether or not the panel is up).
function shouldAdoptAiSession(state, evt) {
  return !isAiOpen() && !evt.payload?.done && !state.analysisTransitionInFlight;
}

// Adopt a session started elsewhere: open the panel and replay what we missed.
function adoptRemoteAiSession(state, ai, aiCtx, evt) {
  ai.rehydrating = true;
  ai.liveBuffer.push(evt);
  // Our cached model name predates the remote start; re-read so the title
  // names the model actually running.
  refreshSettings(state).then(() => setAiTitle(state.aiTitleModel));
  rehydrateAiPanel(ai, aiCtx, { adopt: true });
}

const PLAY_PERSPECTIVE_HTML = `
  <section id="play-perspective">
    <div class="play-grid">
      <div class="play-dock-left"></div>
      <div id="no-engine-banner" class="no-engine-banner hidden" role="status">
        <span class="no-engine-banner__msg">No engine configured.</span>
        <button type="button" class="no-engine-banner__btn" aria-label="Open engine settings" title="Open engine settings">
          <wa-icon name="gear"></wa-icon>
        </button>
      </div>
      <div class="play-board-host"></div>
      <div class="play-ai-inline inline-empty" aria-label="AI analysis"></div>

      <div id="board-controls" class="board-ribbon">
        <button id="new-game" class="ribbon-btn" aria-label="New game" title="New game">
          <wa-icon name="plus"></wa-icon>
        </button>
        <button id="import-pos" class="ribbon-btn desktop-only" aria-label="Open position from FEN or PGN" title="Open">
          <wa-icon name="folder-open"></wa-icon>
        </button>
        <button id="edit-pos" class="ribbon-btn" aria-label="Edit position" title="Edit position">
          <wa-icon name="pencil"></wa-icon>
        </button>
        <button id="save-pgn" class="ribbon-btn desktop-only" aria-label="Save game as PGN" title="Save PGN">
          <wa-icon name="download"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button id="takeback" class="ribbon-btn" disabled aria-label="Take back" title="Take back">
          <wa-icon name="rotate-left"></wa-icon>
        </button>
        <button id="pause" class="ribbon-btn" disabled aria-label="Pause" title="Pause">
          <wa-icon name="pause"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button id="analyze" class="ribbon-btn" disabled aria-label="Analysis mode" title="Analysis mode">
          <wa-icon name="magnifying-glass-plus"></wa-icon>
        </button>
        <button id="switch-sides" class="ribbon-btn" disabled aria-label="Switch sides" title="Switch sides">
          <wa-icon name="arrows-rotate"></wa-icon>
        </button>
        <button id="resign" class="ribbon-btn ribbon-btn--danger" disabled aria-label="Resign" title="Resign">
          <wa-icon name="flag"></wa-icon>
        </button>
        <span class="ribbon-sep ribbon-sep--push desktop-only" aria-hidden="true"></span>
        <button id="pv-table-btn" class="ribbon-btn desktop-only" aria-label="Search Lines" title="Search Lines">
          <wa-icon name="table-list"></wa-icon>
        </button>
        <button id="uci-log-btn" class="ribbon-btn desktop-only" aria-label="UCI log" title="UCI log">
          <wa-icon name="terminal"></wa-icon>
        </button>
      </div>

      <div id="view-controls" class="board-ribbon" style="display: none">
        <button id="view-new-game" class="ribbon-btn" aria-label="New game" title="New game">
          <wa-icon name="plus"></wa-icon>
        </button>
        <button id="view-import" class="ribbon-btn desktop-only" aria-label="Open another position" title="Open">
          <wa-icon name="folder-open"></wa-icon>
        </button>
        <button id="view-edit" class="ribbon-btn" aria-label="Edit position" title="Edit position">
          <wa-icon name="pencil"></wa-icon>
        </button>
        <button id="view-save-pgn" class="ribbon-btn desktop-only" aria-label="Save game as PGN" title="Save PGN">
          <wa-icon name="download"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button id="view-first" class="ribbon-btn" aria-label="First move" title="First move">
          <wa-icon name="backward-fast"></wa-icon>
        </button>
        <button id="view-back" class="ribbon-btn" aria-label="Previous move" title="Previous move">
          <wa-icon name="backward-step"></wa-icon>
        </button>
        <button id="view-forward" class="ribbon-btn" aria-label="Next move" title="Next move">
          <wa-icon name="forward-step"></wa-icon>
        </button>
        <button id="view-last" class="ribbon-btn" aria-label="Last move" title="Last move">
          <wa-icon name="forward-fast"></wa-icon>
        </button>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button id="view-analyze" class="ribbon-btn" aria-label="Analysis mode" title="Analysis mode">
          <wa-icon name="magnifying-glass-plus"></wa-icon>
        </button>
        <button id="view-flip" class="ribbon-btn" aria-label="Flip board" title="Flip board">
          <wa-icon name="arrows-rotate"></wa-icon>
        </button>
        <button id="view-play-from-here" class="ribbon-btn" aria-label="Play from here" title="Play from here">
          <wa-icon name="play"></wa-icon>
        </button>
      </div>

      <div id="edit-controls" class="board-ribbon" style="display: none">
        <div class="side-popover-wrap">
          <button id="edit-side" class="ribbon-btn" aria-label="Side to move" title="Side to move" aria-haspopup="true" aria-expanded="false">
            <wa-icon name="circle-half-stroke"></wa-icon>
          </button>
          <div id="edit-side-popover" class="side-popover hidden" role="dialog" aria-label="Side to move">
            <button type="button" id="edit-side-toggle" class="castle-pill side-toggle-pill" aria-pressed="true">White to move</button>
          </div>
        </div>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <div class="castle-popover-wrap">
          <button id="edit-castle-btn" class="ribbon-btn" aria-label="Castling rights" title="Castling rights" aria-haspopup="true" aria-expanded="false">
            <wa-icon name="chess-rook"></wa-icon>
          </button>
          <div id="edit-castle-popover" class="castle-popover hidden" role="dialog" aria-label="Castling rights">
            <div class="castle-row" data-color="white">
              <span class="castle-row-label">White</span>
              <button type="button" id="edit-castle-cb-wk" class="castle-pill" aria-pressed="false">O-O</button>
              <button type="button" id="edit-castle-cb-wq" class="castle-pill" aria-pressed="false">O-O-O</button>
            </div>
            <div class="castle-row" data-color="black">
              <span class="castle-row-label">Black</span>
              <button type="button" id="edit-castle-cb-bk" class="castle-pill" aria-pressed="false">O-O</button>
              <button type="button" id="edit-castle-cb-bq" class="castle-pill" aria-pressed="false">O-O-O</button>
            </div>
          </div>
        </div>
        <span class="ribbon-sep" aria-hidden="true"></span>
        <button id="edit-flip" class="ribbon-btn" aria-label="Flip board" title="Flip board">
          <wa-icon name="arrows-rotate"></wa-icon>
        </button>
        <button id="edit-annotate" class="ribbon-btn" aria-label="Edit annotation" title="Edit annotation">
          <wa-icon name="align-left"></wa-icon>
        </button>
        <span class="ribbon-sep ribbon-sep--push" aria-hidden="true"></span>
        <button id="edit-confirm" class="ribbon-btn" aria-label="Confirm position" title="Confirm">
          <wa-icon name="check"></wa-icon>
        </button>
        <button id="edit-cancel" class="ribbon-btn" aria-label="Cancel editing" title="Cancel">
          <wa-icon name="xmark"></wa-icon>
        </button>
      </div>

      <div class="play-side-host"></div>
    </div>
  </section>
`;

// X-game (cross-game fork) navigation toasts. All read/write the shared
// `state` (scalars + state.xgame + state.view/state.api).

function closeXgameToasts(state) {
  if (state.xgame.parentToastHandle) { state.xgame.parentToastHandle(); state.xgame.parentToastHandle = null; }
  if (state.xgame.childrenToastHandle) { state.xgame.childrenToastHandle(); state.xgame.childrenToastHandle = null; }
}
function resetXgame(state) {
  closeXgameToasts(state);
  state.xgame.gameId = null;
  state.xgame.parentGameId = null;
  state.xgame.parentSummary = null;
  state.xgame.forkPly = null;
  state.xgame.children = [];
  state.xgame.parentToastDismissed = false;
  state.xgame.childrenToastDismissed = false;
}
async function fetchXgameInfo(state, gameId) {
  if (!gameId) {
    resetXgame(state);
    refreshXgameToasts(state);
    return;
  }
  try {
    const r = await state.api(
      "GET", `/game/recent-imports/by-id/${encodeURIComponent(gameId)}`,
    );
    state.xgame.gameId = gameId;
    state.xgame.parentGameId = r.parent_game_id ?? null;
    state.xgame.parentSummary = r.parent_summary ?? null;
    state.xgame.forkPly = r.fork_ply ?? null;
    state.xgame.children = Array.isArray(r.children) ? r.children : [];
    // Hydrate per-game don't-nag flags from the module-level map so
    // an explicit X survives perspective remount within the same
    // page load.
    const d = _getXgameDismissed(gameId);
    state.xgame.parentToastDismissed = d.parent;
    state.xgame.childrenToastDismissed = d.children;
    // After data lands, re-render the move list so glyphs appear
    // without waiting for the next board_update. Direct apply bypasses
    // the bus handler, so restore the AI arrow it just wiped.
    if (_cachedBoardUpdate) {
      state.view.applyEvent(_cachedBoardUpdate);
      reapplyAiRecommendation(state, _cachedBoardUpdate.payload.fen);
    }
    refreshXgameToasts(state);
  } catch (_e) {
    // The current game may not be in recents (e.g. brand-new play
    // game with no moves yet). That's expected; just clear state.
    resetXgame(state);
    refreshXgameToasts(state);
  }
}
function buildParentToast(state) {
  // "Forked from <parent> at ply N." Single clickable link, X to
  // dismiss. No collapse -- there is only one parent.
  const node = document.createElement("div");
  node.className = "xgame-toast";
  const icon = document.createElement("wa-icon");
  icon.setAttribute("name", "code-fork");
  icon.className = "xgame-toast-icon";
  node.append(icon);
  const text = document.createElement("span");
  text.className = "toast-grow";
  text.append(MSG.FORKED_FROM);
  const link = document.createElement("button");
  link.className = "xgame-link";
  link.type = "button";
  link.textContent = state.xgame.parentSummary
    ? formatGameLabel(state.xgame.parentSummary)
    : "parent game";
  link.addEventListener("click", () => {
    if (state.analyzing) return;
    if (state.xgame.parentGameId) {
      openXgameTarget(state, state.xgame.parentGameId, { landAtPly: state.xgame.forkPly });
    }
  });
  text.append(link);
  text.append(` at ply ${state.xgame.forkPly ?? "?"}.`);
  node.append(text);
  node.append(makeToastDismissBtn(() => {
    state.xgame.parentToastDismissed = true;
    _setXgameDismissed(state.xgame.gameId, "parent", true);
    if (state.xgame.parentToastHandle) {
      state.xgame.parentToastHandle();
      state.xgame.parentToastHandle = null;
    }
  }));
  return node;
}
function buildChildrenToast(state, childrenHere) {
  // "N variation(s) from this position" header with an expand
  // arrow + X. Expansion grows upward (toast is bottom-anchored).
  const node = document.createElement("div");
  node.className = "xgame-toast xgame-toast-collapsible";
  // Expansion list (rendered above the header via flex-direction).
  const list = document.createElement("div");
  list.className = "xgame-toast-list hidden";
  for (const c of childrenHere) {
    const item = document.createElement("button");
    item.className = "xgame-link xgame-toast-list-item";
    item.type = "button";
    item.textContent = formatGameLabel(c.summary);
    item.addEventListener("click", () => {
      if (state.analyzing) return;
      openXgameTarget(state, c.game_id, { landAtPly: c.fork_ply });
    });
    list.append(item);
  }
  node.append(list);
  // Header row.
  const header = document.createElement("div");
  header.className = "xgame-toast-header";
  const icon = document.createElement("wa-icon");
  icon.setAttribute("name", "code-fork");
  icon.className = "xgame-toast-icon";
  header.append(icon);
  const text = document.createElement("span");
  text.className = "toast-grow";
  text.textContent = childrenHere.length === 1
    ? "1 variation from this position"
    : `${childrenHere.length} variations from this position`;
  header.append(text);
  const arrow = document.createElement("button");
  arrow.className = "xgame-toast-arrow";
  arrow.type = "button";
  arrow.setAttribute("aria-label", MSG.SHOW_VARIATIONS);
  arrow.title = MSG.SHOW_VARIATIONS;
  const arrowIcon = document.createElement("wa-icon");
  arrowIcon.setAttribute("name", "chevron-up");
  arrow.append(arrowIcon);
  arrow.addEventListener("click", () => {
    const expanded = !list.classList.toggle("hidden");
    arrowIcon.setAttribute("name", expanded ? "chevron-down" : "chevron-up");
  });
  header.append(arrow);
  header.append(makeToastDismissBtn(() => {
    state.xgame.childrenToastDismissed = true;
    _setXgameDismissed(state.xgame.gameId, "children", true);
    if (state.xgame.childrenToastHandle) {
      state.xgame.childrenToastHandle();
      state.xgame.childrenToastHandle = null;
    }
  }));
  node.append(header);
  return node;
}
function refreshXgameToasts(state) {
  // Reached via async callers (fetchXgameInfo); post-unmount these sticky
  // toasts would orphan -- closeXgameToasts already ran.
  if (state.unmounted) return;
  // Child -> parent: cursor lands precisely on the fork ply of the
  // current child + the game has a parent. Auto-close on ply
  // change (does NOT count as a dismiss).
  const showParent = state.viewing
    && state.xgame.parentGameId
    && state.xgame.forkPly != null
    && state.viewCursor === state.xgame.forkPly
    && state.lastViewNavKind === "precise"
    && !state.xgame.parentToastDismissed;
  if (showParent && !state.xgame.parentToastHandle) {
    state.xgame.parentToastHandle = toast(buildParentToast(state), {
      variant: "neutral", duration: 0, stack: "xgame",
    });
  } else if (!showParent && state.xgame.parentToastHandle) {
    state.xgame.parentToastHandle();
    state.xgame.parentToastHandle = null;
  }
  // Parent -> child: cursor lands precisely on a fork ply that has
  // 1+ children. Same auto-close-on-ply-change rule.
  const childrenHere = (state.xgame.children || []).filter(
    c => (c.fork_ply ?? -1) === state.viewCursor,
  );
  const showChildren = state.viewing
    && state.lastViewNavKind === "precise"
    && childrenHere.length > 0
    && !state.xgame.childrenToastDismissed;
  if (showChildren && !state.xgame.childrenToastHandle) {
    state.xgame.childrenToastHandle = toast(buildChildrenToast(state, childrenHere), {
      variant: "neutral", duration: 0, stack: "xgame",
    });
  } else if (!showChildren && state.xgame.childrenToastHandle) {
    state.xgame.childrenToastHandle();
    state.xgame.childrenToastHandle = null;
  }
}
async function openXgameTarget(state, gameId, opts = {}) {
  // Fetch the target's text from recents, then drive a normal import
  // (server-side enter_view_mode swap). landAtPly: optional cursor ply to
  // land on after import (fork_ply for both directions; see x-game-navigation.md).
  const landAtPly = opts.landAtPly ?? null;
  // Short-circuit: already viewing this game at the target ply.
  // A re-import would re-animate cm-chessboard to the same
  // position (visible flicker for the user).
  if (
    state.viewing
    && state.viewingGameId === gameId
    && (landAtPly === null || state.viewCursor === landAtPly)
  ) {
    return true;
  }
  try {
    const target = await state.api(
      "GET", `/game/recent-imports/by-id/${encodeURIComponent(gameId)}`,
    );
    // Pass land_at_ply in the import payload so the server enters
    // view mode at the target ply in a SINGLE transaction (no
    // follow-up /view/goto -> no animation flicker).
    const importPayload = { format: target.format, text: target.text };
    if (landAtPly !== null && landAtPly > 0) {
      importPayload.land_at_ply = landAtPly;
      // Precise landing on the fork ply -> any banner gated on
      // precise nav can fire on the resulting board_update.
      state.lastViewNavKind = "precise";
    }
    // GameView's applyEvent drops board_updates whose game_id does
    // NOT match its local gameId. Clear before import so the
    // server's fresh game_id is accepted; set it to the returned
    // id so subsequent updates are still scoped.
    state.view.setGameId(null);
    closeAi();
    const r = await state.api("POST", "/game/import", importPayload);
    if (r?.game_id) state.view.setGameId(r.game_id);
    return true;
  } catch (e) {
    reportError(state.ctx, MSG.OPEN_GAME_FAILED, e);
    return false;
  }
}

// Confirm/import/nav helpers operating on the shared `state`.

// Prompt before discarding an active play game. Returns true if the
// caller should proceed (no active game, or user confirmed).
async function _confirmDiscardActiveGame({ message, okLabel }) {
  if (!_playInProgress) return true;
  return await confirm({
    message,
    okLabel,
    cancelLabel: MSG.KEEP_PLAYING,
    destructive: true,
  });
}

// Prompt before replacing the currently viewed game. Skips when nothing
// is being viewed or when the incoming hash matches the current view.
async function _confirmReplaceViewedGame(state, { incomingHash, incomingSummary }) {
  if (!state.viewing) return true;
  return await confirmReplaceViewedGame({
    currentHash: _viewingHash,
    currentSummary: _viewingSummary,
    incomingHash,
    incomingSummary,
    analysisRunning: state.analyzing,
  });
}

// Tracks how the cursor reached the next ply: "precise" (back / forward /
// goto / move-list click) vs "jump" (first / last). The parent->child banner
// only fires on precise landings -- jumping over a fork must NOT pop a prompt.
async function doViewNav(state, endpoint, payload = {}) {
  state.lastViewNavKind =
    endpoint === "/game/view/first" || endpoint === "/game/view/last"
      ? "jump"
      : "precise";
  try {
    await state.ctx.api("POST", endpoint, payload);
  } catch (e) {
    reportError(state.ctx, MSG.NAV_FAILED, e);
  }
}

// Analysis state setter + stop flow operating on shared `state`.

// Single sync point: every analyzing write goes through this setter so the
// AI-finished latch and the x-game lock class stay consistent.
// Direct `state.analyzing = ...` writes will drift -- always call setAnalyzing.
function setAnalyzing(state, v) {
  const was = state.analyzing;
  state.analyzing = !!v;
  // Server flipped out of ANALYSIS -- clear the AI-finished latch
  // so the ribbon can re-enable when the game is paused again.
  if (!state.analyzing) state.aiShared.turnFinished = false;
  document.body.classList.toggle(XGAME_LOCK_CLASS, state.analyzing);
  // The session is server-owned: whoever ended it, every client drops the
  // panel -- its replay buffer is gone, so it can't be restored anyway.
  if (was && !state.analyzing && !state.analysisTransitionInFlight) teardownAiPanel(state.aiShared);
}

// Re-apply the stashed AI recommendation arrow after a board_update wiped
// the arrows (GameView clears them on every apply). Same-FEN guard: a real
// move correctly drops the stale arrow. Cleared once analysis ends. Shared
// by the bus handler and fetchXgameInfo's direct cached-update re-apply.
function reapplyAiRecommendation(state, fen) {
  const rec = state.aiShared.recommendation;
  if (!rec) return;
  if (state.analyzing && rec.fen === fen) {
    state.view.applyEvent(rec.evt);
  } else if (!state.analyzing) {
    state.aiShared.recommendation = null;
  }
}

// Stop side of the analyze toggle, shared so the AI-window close handler can
// trigger the same flow (snapshot + endpoint + toast + panels) as the toolbar
// Stop button. Returns false when the stop POST failed (error already
// toasted) so callers chaining resume/move can bail.
async function stopAnalysisFromUi(state) {
  if (!state.analyzing) return true;
  snapshotViewAnalysisState();
  state.analysisTransitionInFlight = true;
  try {
    await state.ctx.api("POST", "/game/analysis/stop", {});
  } catch (e) {
    reportError(state.ctx, MSG.STOP_ANALYSIS_FAILED, e);
    return false;
  } finally {
    state.analysisTransitionInFlight = false;
  }
  state.aiShared.turnFinished = false;
  // The AI window's lifecycle is tied to the analysis session, so it
  // always closes on stop. PV/UCI close only if analysis opened them.
  teardownAiPanel(state.aiShared);
  return true;
}

// Edit-mode side/castling popover handlers. Operate on the shared `state`
// (state.el DOM refs, state.view, state.refreshButtons, state.editing).

// True when the ribbon is in floating (WinBox) mode -- body[data-ribbon-float]
// is the source of truth (cleared on mobile where float is suppressed).
function _ribbonFloating() {
  return !!document.body.dataset.ribbonFloat;
}

// Portal `popover` to <body> and position it (position: fixed) beside the
// floating WinBox. Horizontal anchor is the WHOLE window's edge (not the
// button) so the popover clears the window instead of overlapping it; vertical
// anchor tracks the button. Used only in floating mode; docked mode keeps
// CSS-anchored popovers.
function _floatPopover(btn, popover) {
  document.body.appendChild(popover);
  popover.classList.add(POPOVER_FLOATING_CLASS);
  popover.style.left = "0px";
  popover.style.top = "0px";
  const b = btn.getBoundingClientRect();
  // Anchor horizontally to the WinBox so the popover sits outside it. Fall
  // back to the button rect if the window element can't be found.
  const wbEl = btn.closest(".winbox");
  const anchor = wbEl ? wbEl.getBoundingClientRect() : b;
  const pw = popover.offsetWidth;
  const ph = popover.offsetHeight;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = anchor.right + POPOVER_GAP;
  if (left + pw > vw) left = anchor.left - POPOVER_GAP - pw; // flip left of window
  left = Math.max(POPOVER_GAP, Math.min(left, vw - pw - POPOVER_GAP));
  let top = b.top; // vertical alignment stays with the trigger button
  if (top + ph > vh) top = b.bottom - ph; // anchor bottom edge instead
  top = Math.max(POPOVER_GAP, Math.min(top, vh - ph - POPOVER_GAP));
  popover.style.left = `${left}px`;
  popover.style.top = `${top}px`;
}

// Undo _floatPopover: strip fixed-position styling and return the popover to
// its wrap so the docked CSS rules apply again.
function _unfloatPopover(popover, wrap) {
  popover.classList.remove(POPOVER_FLOATING_CLASS);
  popover.style.left = "";
  popover.style.top = "";
  if (popover.parentNode !== wrap) wrap.appendChild(popover);
}

function _closeSidePopover(state) {
  state.el.editSidePopover.classList.add("hidden");
  state.el.editSideBtn.setAttribute("aria-expanded", "false");
  _unfloatPopover(state.el.editSidePopover, state.el.editSidePopoverWrap);
}
function _closeCastlePopover(state) {
  state.el.editCastlePopover.classList.add("hidden");
  state.el.editCastleBtn.setAttribute("aria-expanded", "false");
  _unfloatPopover(state.el.editCastlePopover, state.el.editCastlePopoverWrap);
}
// Close both edit-mode popovers (e.g. on viewport resize or WinBox drag,
// where repositioning a floated popover would be more churn than value).
function _closeEditPopovers(state) {
  _closeSidePopover(state);
  _closeCastlePopover(state);
}
function editSidePopoverToggle(state, ev) {
  ev.stopPropagation();
  if (!state.el.editSidePopover.classList.contains("hidden")) {
    _closeSidePopover(state);
  } else {
    _closeCastlePopover(state); // popovers are mutually exclusive
    state.el.editSidePopover.classList.remove("hidden");
    state.el.editSideBtn.setAttribute("aria-expanded", "true");
    if (_ribbonFloating()) _floatPopover(state.el.editSideBtn, state.el.editSidePopover);
  }
}
function editSideFlip(state) {
  state.view.setEditSide(state.view.getEditSide() === FEN_STM.WHITE ? FEN_STM.BLACK : FEN_STM.WHITE);
  state.refreshButtons();
}
function editCastleToggle(state, right) {
  state.view.toggleCastlingRight(right);
  state.refreshButtons();
}
function editCastlePopoverToggle(state, ev) {
  ev.stopPropagation();
  if (!state.el.editCastlePopover.classList.contains("hidden")) {
    _closeCastlePopover(state);
  } else {
    _closeSidePopover(state); // popovers are mutually exclusive
    state.el.editCastlePopover.classList.remove("hidden");
    state.el.editCastleBtn.setAttribute("aria-expanded", "true");
    if (_ribbonFloating()) _floatPopover(state.el.editCastleBtn, state.el.editCastlePopover);
  }
}
function editDocClickClose(state, ev) {
  if (!state.editing) return;
  const { editCastlePopover, editCastleBtn, editSidePopover, editSideBtn } = state.el;
  if (!editCastlePopover.classList.contains("hidden") &&
      !editCastlePopover.contains(ev.target) && !editCastleBtn.contains(ev.target)) {
    _closeCastlePopover(state);
  }
  if (!editSidePopover.classList.contains("hidden") &&
      !editSidePopover.contains(ev.target) && !editSideBtn.contains(ev.target)) {
    _closeSidePopover(state);
  }
}

// Pull settings into `state`; on notifyOnDrift, toast TC changes that will
// only apply next game. Refreshes the TC snapshot when no game is active.
async function refreshSettings(state, { notifyOnDrift = false } = {}) {
  try {
    const s = await state.ctx.api("GET", "/settings");
    state.allowTakeback = s.allow_takeback !== false;
    state.showPgnComments = s[SETTING_SHOW_PGN_COMMENTS] !== false;
    state.aiEnabled = !!s.ai_enabled;
    state.aiTitleModel = s.ai_enabled ? (s.ai_model || "") : "";
    syncCommentsVisibility(state);
    setEvalGraphEnabled(s[SETTING_SHOW_EVAL_GRAPH] !== false);
    if (notifyOnDrift && !state.gameOver && state.resignAvailable) {
      const drift = [];
      // TC: compare against the snapshot taken at game start.
      if (
        state.gameTcInitial !== null &&
        (Number(s.tc_initial_seconds) !== state.gameTcInitial ||
          Number(s.tc_increment_seconds) !== state.gameTcIncrement)
      ) {
        drift.push("time control");
      }
      if (drift.length > 0) {
        toast(
          `New ${drift.join(" and ")} will apply on the next game.`,
          { variant: "neutral" },
        );
      }
    }
    // Always refresh the snapshot from current settings when there is
    // no active game (so the "next game" comparison is accurate).
    if (!state.resignAvailable) {
      state.gameTcInitial = Number(s.tc_initial_seconds);
      state.gameTcIncrement = Number(s.tc_increment_seconds);
    }
  } catch {
    // ignore
  }
}

// Ribbon button state, computed from the shared `state`.

// Board input: on in live play, and during play-mode analysis (a drop
// offers to stop analysis and play the move). Off while paused or viewing.
function syncBoardInputEnabled(state) {
  if (state.viewing) return;
  state.view.setEnabled(state.analyzing || !state.paused);
}

// Paused overlay + badge (hidden while analyzing, which has its own affordance).
function syncPausedUi(state) {
  const show = state.paused && !state.analyzing;
  state.el.boardHost.classList.toggle("board-paused", show);
  state.el.pausedBadge?.classList.toggle("hidden", !show);
}

// Restart the one-shot pulse on `el`: drop the class, force reflow, re-add so
// rapid repeat clicks always replay the animation. Self-removes on end.
function _pulseOnce(el) {
  if (!el) return;
  // No animation under reduced-motion -> animationend never fires; skip so the
  // class + listener don't leak.
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  el.classList.remove(PAUSE_HINT_PULSE_CLASS);
  void el.offsetWidth; // reflow so re-adding the class restarts the animation
  el.classList.add(PAUSE_HINT_PULSE_CLASS);
  el.addEventListener(
    "animationend",
    () => el.classList.remove(PAUSE_HINT_PULSE_CLASS),
    { once: true },
  );
}

// User clicked the inert paused board: pulse the Paused badge + Resume button
// to point them at how to resume. No-op unless actually paused.
function hintResumeFromPausedBoard(state) {
  if (!state.paused || state.analyzing) return;
  _pulseOnce(state.el.pausedBadge);
  _pulseOnce(state.el.pauseBtn);
}

function showFinishedBadge(state, text) {
  if (!state.el.finishedBadge) return;
  state.el.finishedBadge.textContent = text;
  state.el.finishedBadge.classList.toggle("hidden", !text);
}

// A finished AI-analysis turn in play mode: board frozen in ANALYZING, reads as
// paused, so the ribbon shows Resume (one click exits analysis + resumes play).
// Reads live state -- call, don't cache.
function aiAnalysisDone(state) {
  return state.analyzing && state.aiShared.turnFinished && state.aiEnabled;
}

// An AI analysis turn is actually running. aiEnabled excludes engine-only
// analysis runs that may happen under a stale-open AI panel.
function aiTurnInFlight(state) {
  return state.aiEnabled && state.analyzing && !state.aiShared.turnFinished;
}

function refreshButtons(state) {
  // Swap ribbons: edit overrides view, which overrides play.
  const activeRibbon = state.editing ? state.el.editRibbon : state.viewing ? state.el.viewRibbon : state.el.playRibbon;
  state.el.playRibbon.style.display = (state.viewing || state.editing) ? "none" : "";
  state.el.viewRibbon.style.display = (state.viewing && !state.editing) ? "" : "none";
  state.el.editRibbon.style.display = state.editing ? "" : "none";
  window.dispatchEvent(new CustomEvent(APP_EVT.RIBBON_ACTIVE, { detail: { el: activeRibbon } }));
  if (state.editing) {
    const isWhite = state.view.getEditSide() === FEN_STM.WHITE;
    state.el.editSideBtn.setAttribute("aria-label", `Side to move: ${isWhite ? "White" : "Black"}`);
    state.el.editSideBtn.setAttribute("title", `Side to move: ${isWhite ? "White" : "Black"}`);
    state.el.editSideBtn.classList.toggle("is-active", !isWhite);
    state.el.editSideTogglePill.textContent = isWhite ? MSG.WHITE_TO_MOVE : MSG.BLACK_TO_MOVE;
    state.el.editSideTogglePill.classList.toggle("is-black", !isWhite);
    state.el.editSideTogglePill.setAttribute("aria-pressed", isWhite ? "false" : "true");
    const rights = state.view.getCastlingRights();
    for (const [k, btn] of Object.entries(state.el.editCastleCb)) {
      btn.setAttribute("aria-pressed", rights[k] ? "true" : "false");
      btn.classList.toggle("is-active", rights[k]);
    }
    const anyRight = rights.wK || rights.wQ || rights.bK || rights.bQ;
    state.el.editCastleBtn.classList.toggle("is-active", anyRight);
    return;
  }
  if (state.viewing) {
    const atStart = state.viewCursor === 0;
    const atEnd = state.viewCursor === state.viewTotalPlies;
    configureBtn(state.el.viewFirstBtn, { disabled: state.analyzing || atStart });
    configureBtn(state.el.viewBackBtn, { disabled: state.analyzing || atStart });
    configureBtn(state.el.viewForwardBtn, { disabled: state.analyzing || atEnd });
    configureBtn(state.el.viewLastBtn, { disabled: state.analyzing || atEnd });
    configureBtn(state.el.viewSavePgnBtn, { disabled: state.analyzing || state.viewTotalPlies === 0 });
    // Play-from-here is rejected at game-over plies (checkmate /
    // stalemate / draw). Backed by a backend guard that prevents
    // half-cleared state if the UI is bypassed.
    configureBtn(state.el.viewPlayFromHereBtn, { disabled: state.analyzing || state.viewGameOver });
    // Engine-less view: analyze is unreachable. Tooltip points at
    // Engines tab so the user knows the next step.
    // AI turn finished but server still ANALYZING: show ribbon as
    // normal ("Analysis mode") even though `analyzing` is true.
    const viewShowAsActive = state.analyzing && !state.aiShared.turnFinished;
    configureBtn(state.el.viewAnalyzeBtn, {
      disabled: (state.noEngine || state.viewPositionTerminal) && !viewShowAsActive,
      active: viewShowAsActive,
      label: viewShowAsActive
        ? MSG.STOP_ANALYSIS
        : state.noEngine
          ? MSG.ANALYZE_NEEDS_ENGINE
          : MSG.ANALYSIS_MODE,
      icon: viewShowAsActive ? ANALYZE_ICON_STOP : ANALYZE_ICON_START,
    });
    return;
  }
  const humanIsToMove = humanToMove(state);
  // Completed AI analysis in play mode reads as paused to the user; show
  // Resume (see aiAnalysisDone / onPause). Engine-only analysis and
  // in-progress runs keep the plain Pause/Resume toggle.
  const aiDone = aiAnalysisDone(state);
  const showResume = state.paused || aiDone;
  // Needs a live game; Pause then needs the human's turn (Resume is always
  // allowed once a game exists, which any showResume state implies).
  configureBtn(state.el.pauseBtn, {
    disabled: !state.resignAvailable || state.gameOver || (!aiDone && state.analyzing) || (!showResume && !humanIsToMove),
    label: showResume ? "Resume" : "Pause",
    icon: showResume ? "forward-step" : "pause",
  });
  configureBtn(state.el.takebackBtn, {
    disabled: state.analyzing || state.gameOver || !state.allowTakeback || state.movesPlayed === 0,
  });
  // Saving mid-game pauses first (see onSavePgnImpl), and pause needs the
  // human's turn -- so gate on it whenever that pause would be required.
  const savePauseBlocked =
    state.resignAvailable && !state.paused && !state.gameOver && !humanIsToMove;
  configureBtn(state.el.savePgnBtn, {
    disabled: state.analyzing || state.movesPlayed === 0 || savePauseBlocked,
  });
  configureBtn(state.el.switchSidesBtn, { disabled: state.analyzing || state.gameOver || !state.resignAvailable });
  configureBtn(state.el.resignBtn, { disabled: state.paused || state.analyzing || state.gameOver || !state.resignAvailable });
  // AI turn done but server still ANALYZING: show the button as normal
  // "Analysis mode" (enabled); the reachability gates below still apply.
  const showAsActive = state.analyzing && !state.aiShared.turnFinished;
  const analyzeReachable = !state.gameOver && state.resignAvailable && (state.paused || state.aiShared.turnFinished);
  configureBtn(state.el.analyzeBtn, {
    disabled: !showAsActive && !analyzeReachable,
    active: showAsActive,
    label: showAsActive ? MSG.STOP_ANALYSIS : MSG.ANALYSIS_MODE,
    icon: showAsActive ? ANALYZE_ICON_STOP : ANALYZE_ICON_START,
  });
}

// Play/edit action handlers. Thin mount thunks delegate here with `state`.

async function onResignImpl(state) {
  const ok = await confirm({
    message: MSG.CONFIRM_RESIGN,
    okLabel: MSG.RESIGN,
    cancelLabel: MSG.KEEP_PLAYING,
    destructive: true,
  });
  if (!ok) return;
  try {
    await state.ctx.api("POST", "/game/resign", {});
  } catch (e) {
    reportError(state.ctx, MSG.RESIGN_FAILED, e);
  }
}

async function onSavePgnImpl(state) {
  const needsPause = !state.viewing && !state.paused && !state.gameOver && state.resignAvailable;
  if (needsPause) {
    try { await state.ctx.api("POST", "/game/pause", {}); } catch (e) {
      reportError(state.ctx, MSG.SAVE_PGN_FAILED, e);
      return;
    }
  }
  try {
    const r = await fetch("/game/pgn");
    if (!r.ok) {
      const detail = await r.text();
      throw new Error(`GET /game/pgn -> ${r.status} ${detail}`);
    }
    const cd = r.headers.get("Content-Disposition") || "";
    const match = cd.match(/filename="([^"]+)"/);
    const filename = match ? match[1] : "game.pgn";
    const bridge = window.pywebview && window.pywebview.api && window.pywebview.api.save_pgn;
    if (bridge) {
      // Desktop (PyWebView/WebView2): blob downloads don't trigger a
      // save dialog, so route through the native bridge instead.
      const text = await r.text();
      const res = await window.pywebview.api.save_pgn(text, filename);
      if (res && res.ok) {
        toast(`Saved to ${res.path}`, { variant: "success" });
      } else if (res && res.cancelled) {
        // user dismissed dialog; stay silent
      } else {
        throw new Error((res && res.error) || "save failed");
      }
    } else {
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    }
  } catch (e) {
    reportError(state.ctx, MSG.SAVE_PGN_FAILED, e);
  } finally {
    if (needsPause) {
      try { await state.ctx.api("POST", "/game/resume", {}); } catch (_) { /* best-effort */ }
    }
  }
}

async function onTakebackImpl(state) {
  if (state.takebackPending) return;
  state.takebackPending = true;
  try {
    await state.ctx.api("POST", "/game/takeback", {});
  } catch (e) {
    reportError(state.ctx, MSG.TAKEBACK_FAILED, e);
  } finally {
    state.takebackPending = false;
  }
}

async function onSwitchSidesImpl(state) {
  try {
    await state.ctx.api("POST", "/game/switch-sides", {});
  } catch (e) {
    reportError(state.ctx, MSG.SWITCH_SIDES_FAILED, e);
  }
}

async function onPauseImpl(state) {
  // Completed AI analysis in play mode: Resume exits analysis (server
  // lands in PAUSED) then resumes to PLAY, so one click returns to the
  // game. stopAnalysisFromUi tears down the AI window and replay buffer.
  if (aiAnalysisDone(state)) {
    if (!await stopAnalysisFromUi(state)) return;
    try {
      await state.ctx.api("POST", "/game/resume", {});
    } catch (e) {
      reportError(state.ctx, MSG.RESUME_FAILED, e);
    }
    return;
  }
  try {
    await state.ctx.api("POST", state.paused ? "/game/resume" : "/game/pause", {});
  } catch (e) {
    reportError(state.ctx, state.paused ? MSG.RESUME_FAILED : MSG.PAUSE_FAILED, e);
  }
}

async function onNewGameImpl(state) {
  if (_playInProgress) {
    if (!await _confirmDiscardActiveGame({
      message: MSG.CONFIRM_NEW_GAME,
      okLabel: MSG.NEW_GAME,
    })) return;
  } else {
    const ok = await confirmDiscardViewedGame({
      viewing: state.viewing,
      currentSummary: _viewingSummary,
      analysisRunning: state.analyzing,
    });
    if (!ok) return;
  }
  try {
    // Reset the board BEFORE the POST, then drop the game_id so old-game
    // events stop applying. An instant engine first move (e.g. an opening
    // book move, no search delay) can publish its board_update before the
    // POST response is delivered; resetting afterward would wipe that move
    // back to the start position (the move appears, then "withdraws"). By
    // resetting first and never resetting again, the new game's events --
    // including that early book move -- land on a clean board and persist.
    state.view.reset();
    state.view.setGameId(null);
    closeAi();
    const r = await state.ctx.api("POST", "/game/new", {});
    state.view.setGameId(r.game_id);
    state.view.setHumanWhite(!!r.human_white);
    state.resignAvailable = true;
    // Snapshot the TC settings used for THIS game so a later mid-game
    // edit can detect drift.
    try {
      const s = await state.ctx.api("GET", "/settings");
      state.gameTcInitial = Number(s.tc_initial_seconds);
      state.gameTcIncrement = Number(s.tc_increment_seconds);
    } catch {
      // ignore — drift detection just won't trigger for TC.
    }
    state.refreshButtons();
  } catch (e) {
    reportError(state.ctx, MSG.NEW_GAME_FAILED, e);
  }
}

async function onImportImpl(state) {
  if (!await _confirmDiscardActiveGame({
    message: MSG.CONFIRM_IMPORT,
    okLabel: MSG.IMPORT,
  })) return;
  // Dialog validates (parse errors surface inline) but does not import.
  const result = await showImportPositionDialog({ api: state.ctx.api });
  if (!result) return;
  // Same game already in view -- stay put, no re-import needed.
  if (state.viewing && result.hash && result.hash === _viewingHash) {
    if (state.viewingGameId) toast(`Viewing ${state.viewingGameId}`);
    return;
  }
  // Different game while viewing -- confirm before replacing.
  if (!await _confirmReplaceViewedGame(state, { incomingHash: result.hash, incomingSummary: result.summary })) return;
  try {
    closeAi();
    const r = await state.ctx.api("POST", "/game/import", {
      format: result.format, text: result.text, opening: result.opening || undefined,
    });
    state.view.setGameId(r.game_id);
    state.ctx.api("POST", "/game/sync", {}).catch(() => {});
  } catch (e) {
    reportError(state.ctx, MSG.IMPORT_FAILED, e);
  }
}

function canEnterViewAtPly(state, ply) {
  return !state.analyzing && !state.viewing && ply + 1 < state.movesPlayed;
}

// Play -> view: flip the live play game into server view mode landing at a
// past ply. The live game is suspended server-side (no fork) so scrubbing to
// the last ply can resume it (see resumeLivePlay). Ignores clicks on the
// live last move (already there) and during analysis.
async function enterViewAtPly(state, plyIndex) {
  if (!canEnterViewAtPly(state, plyIndex)) return;
  if (state.enterViewInflight) return;  // debounce double-click (esp. eval bar)
  state.enterViewInflight = true;
  try {
    closeAi();
    // Clear gameId so the view-mode board_update (fresh game_id) isn't
    // dropped by GameView's game_id filter; restore from the response.
    state.view.setGameId(null);
    // suspend:true holds the live game for a no-fork resume at the last ply.
    const r = await state.ctx.api(
      "POST", "/game/view/start", { land_at_ply: plyIndex + 1, suspend: true },
    );
    if (r?.game_id) state.view.setGameId(r.game_id);
  } catch (e) {
    reportError(state.ctx, MSG.OPEN_GAME_FAILED, e);
  } finally {
    state.enterViewInflight = false;
  }
}

// View -> play: resume the SAME suspended play game (no fork). Fired when a
// resumable view session scrubs to its last ply (see handleBusEvent).
async function resumeLivePlay(state) {
  if (state.resumeInflight) return;  // debounce racing board_updates
  state.resumeInflight = true;
  try {
    state.view.setGameId(null);
    const r = await state.ctx.api("POST", "/game/view/resume-play", {});
    if (r?.game_id) state.view.setGameId(r.game_id);
  } catch (e) {
    reportError(state.ctx, MSG.RESUME_FAILED, e);
  } finally {
    state.resumeInflight = false;
  }
}

async function onPlayFromHereImpl(state) {
  if (state.playFromHereInflight) return;  // debounce double-click
  state.playFromHereInflight = true;
  setDisabled(state.el.viewPlayFromHereBtn, true);
  // Reset gameId so the racing board_update from new_game (which fires
  // BEFORE the API response carrying the new id) isn't dropped by the
  // game_id filter — that drop loses the human_white/name swap.
  state.view.setGameId(null);
  try {
    closeAi();
    const r = await state.ctx.api("POST", "/game/view/play-from-here", {});
    state.view.setGameId(r.game_id);
    // Snapshot TC for drift detection (mirrors onNewGame).
    try {
      const s = await state.ctx.api("GET", "/settings");
      state.gameTcInitial = Number(s.tc_initial_seconds);
      state.gameTcIncrement = Number(s.tc_increment_seconds);
    } catch {
      // ignore
    }
  } catch (e) {
    // Server keeps view mode on failure -- restore the gameId filter and
    // re-enable the button so the user can retry from the same position.
    state.view.setGameId(state.viewingGameId);
    setDisabled(state.el.viewPlayFromHereBtn, false);
    reportError(state.ctx, MSG.PLAY_FROM_HERE_FAILED, e);
  } finally {
    state.playFromHereInflight = false;
  }
}

async function onEditCancelImpl(state) {
  try {
    const r = await state.ctx.api("POST", "/game/edit/cancel", {});
    if (r?.game_id) state.view.setGameId(r.game_id);
  } catch (e) {
    reportError(state.ctx, MSG.CANCEL_EDIT_FAILED, e);
  }
}

async function onEditConfirmImpl(state) {
  const fen = state.view.getEditFen();
  // Server mints a fresh game_id on a real position change. Clear the
  // filter so the board_update SSE (which races the POST response) isn't
  // dropped for not matching our stale id.
  state.view.setGameId(null);
  const payload = { fen };
  if (state.pendingAnnotation !== null) {
    payload.apply_comment = true;
    payload.comment_text = state.pendingAnnotation;
  }
  try {
    const r = await state.ctx.api("POST", "/game/edit/commit", payload);
    state.view.setGameId(r.game_id);
    // Annotation-only commit can promote an unsaved fork child to
    // recents (xgame nav "lazy commit"). Game_id is unchanged so
    // the board_update doesn't trigger fetchXgameInfo -- refetch
    // explicitly so the fork glyph + banner state catch up.
    if (r.game_id) fetchXgameInfo(state, r.game_id);
  } catch (e) {
    reportError(state.ctx, MSG.INVALID_POSITION, e);
  }
}

async function onEditAnnotateImpl(state) {
  // Preload from pendingAnnotation (if user already staged something
  // this edit session) or fall back to the server's current comment.
  const preload = state.pendingAnnotation ?? (state.lastViewComment ?? "");
  const result = await editAnnotation({ currentText: preload });
  if (result?.apply) {
    state.pendingAnnotation = result.text;
    // Optimistically reflect the staged text in the commentary dock
    // so the user sees their pending change. Lives until edit-commit
    // (server then makes it real) or edit-cancel (we restore the
    // pre-edit text from lastViewComment).
    if (isCommentaryOpen()) {
      setCommentaryText(state.pendingAnnotation || null);
    }
  }
}

// Analysis toggle cluster. The persistent "Analysis mode" toast wires Search
// Lines / UCI log / Stop; start/stop/reanalyze drive the server + AI panel.

// Dismiss the persistent "Analysis mode" toast and drop its handle.
function dismissAnalysisToast(aiShared) {
  aiShared.dismissAnalysisToast?.();
  aiShared.dismissAnalysisToast = null;
}

// Dismiss a prior round's AI-error toast and drop its handle. A new round
// (local start or cross-client) supersedes any error the last one left up.
function dismissAiErrorToast() {
  _dismissAiErrorToast?.();
  _dismissAiErrorToast = null;
}

function showAnalysisToastImpl(state) {
  dismissAnalysisToast(state.aiShared);
  const msg = document.createElement("span");
  msg.className = "toast-sort-msg";
  const label = document.createElement("span");
  label.className = "toast-grow is-active";
  label.textContent = MSG.ANALYSIS_MODE;
  msg.append(label);
  const pvTableBtn = makeToastIconBtn("table-list", MSG.SEARCH_LINES, () => togglePvTableWindow(state.ctx.events));
  pvTableBtn.classList.add("desktop-only");
  msg.append(pvTableBtn);
  const uciLogBtn = makeToastIconBtn("terminal", MSG.UCI_LOG, () => toggleUciLogWindow(state.ctx.events));
  uciLogBtn.classList.add("desktop-only");
  msg.append(uciLogBtn);
  const stopBtn = makeToastIconBtn(ANALYZE_ICON_STOP, MSG.STOP_ANALYSIS, () => onAnalyzeImpl(state));
  stopBtn.classList.add("is-active");
  msg.append(stopBtn);
  state.aiShared.dismissAnalysisToast = toast(msg, {
    variant: "neutral",
    duration: 0,
  });
}

// POST start + restore panels + toast + open/reset AI panel. Shared by the
// analyze toggle and the re-analyze button so the two paths can't drift.
// openAi() before resetAi(): resetAi sets the spinner and no-ops when null.
// Resolve the analysis engine's display name, mirroring the server's
// resolve_analysis: the pinned analysis_engine_id, else the active engine.
async function resolveAnalysisEngineName(state) {
  const [s, e] = await Promise.all([
    state.ctx.api("GET", "/settings"),
    state.ctx.api("GET", "/engines"),
  ]);
  const engines = e.engines || [];
  const id = s.analysis_engine_id || e.selected_id;
  // Server falls back to the selected engine when the pinned id is gone.
  const eng = engines.find((x) => x.id === id)
    || engines.find((x) => x.id === e.selected_id);
  return eng?.name || "";
}

async function startAnalysisFromUiImpl(state) {
  // Unmounted while a caller (e.g. re-analyze's stop POST) awaited: building
  // the toast/panel now would orphan them -- unmount already ran.
  if (state.unmounted) return;
  // Engine-only analysis: close any leftover AI panel from a prior AI run
  // before starting, so the dock shows engine-only output. Done first so
  // the close can't race the new analysis state.
  if (!state.aiEnabled && isAiOpen()) closeAi();
  state.aiShared.turnFinished = false;
  dismissAiErrorToast();
  clearUciLog();
  // Build ALL start-state (toast + panel) synchronously BEFORE the POST.
  // An instant-fail turn's done/error event arrives during the await; with
  // everything built first it tears it all down cleanly. Setup split across
  // the await leaves a half-built UI (orphan toast/spinner) on that race.
  showAnalysisToastImpl(state);
  if (state.aiEnabled) {
    // Pin the title to the model actually about to run. Mid-session
    // provider/model edits do not retitle until the next Analyze click.
    setAiTitle(state.aiTitleModel);
    openAi();
    resetAi();
  }
  await state.ctx.api("POST", "/game/analysis/start", {});
  // Unmounted mid-POST (user navigated away): unmount already dismissed the
  // toast and dropped the dock container -- opening windows now would float
  // them over the next perspective.
  if (state.unmounted) return;
  // Name the UCI Log after the analysis engine (may differ from the play
  // engine). Async + best-effort so it can't delay or fail the start.
  resolveAnalysisEngineName(state)
    .then((n) => { if (state.analyzing) setUciLogEngine(n); })
    .catch(() => {});
  restoreViewAnalysisWindows(state.ctx.events);
}

// Local teardown (no server call; never POST /analysis/stop back -- the
// server already left analysis or never entered). Unconditionally drops
// the toast: session over, idempotent, start-failures emit no event.
function teardownAiPanel(aiShared) {
  dismissAnalysisToast(aiShared);
  if (isAiOpen()) closeAi();
  closeAnalysisOpenedWindows();
}

async function onAnalyzeImpl(state) {
  // A second tap before the start round trip lands would start a second
  // session: the server no-ops the mode change but still supersedes the
  // in-flight AI turn, dropping its [cancelled] marker into the new panel.
  if (state.analysisTransitionInFlight) return;
  // Switching from a FINISHED AI session to engine-only: stop the AI session,
  // then start engine analysis -- a plain Stop would tear down and leave
  // nothing running. While the AI run is in progress the ribbon is a plain Stop.
  if (state.analyzing && state.aiShared.turnFinished && !state.aiEnabled && isAiOpen()) {
    await onReanalyzeImpl(state);
    return;
  }
  if (state.analyzing) {
    await stopAnalysisFromUi(state);
    return;
  }
  state.analysisTransitionInFlight = true;
  try {
    await startAnalysisFromUiImpl(state);
  } catch (e) {
    teardownAiPanel(state.aiShared);
    _dismissAiErrorToast = reportError(state.ctx, MSG.START_ANALYSIS_FAILED, e, { duration: 0 });
  } finally {
    state.analysisTransitionInFlight = false;
  }
}

// Re-analyze: stop the current turn server-side (if any), then start a fresh
// one, keeping the AI panel open. analysisTransitionInFlight guards against
// rapid double-clicks producing a spurious second start (server ->
// ModeConflictError), and keeps the stop's own events from closing the panel.
async function onReanalyzeImpl(state) {
  if (state.analysisTransitionInFlight) return;
  state.analysisTransitionInFlight = true;
  try {
    if (state.analyzing) {
      snapshotViewAnalysisState();
      await state.ctx.api("POST", "/game/analysis/stop", {});
    }
    await startAnalysisFromUiImpl(state);
  } catch (e) {
    teardownAiPanel(state.aiShared);
    _dismissAiErrorToast = reportError(state.ctx, MSG.REANALYZE_FAILED, e, { duration: 0 });
  } finally {
    state.analysisTransitionInFlight = false;
  }
}

const VIEW_FLIP_KEY = STORAGE_KEY.VIEW_FLIPPED;

// View-mode flip is purely visual (no backend state; the user isn't playing
// yet so "which side am I" is meaningless). Persisted across remounts.
function onViewFlipImpl(state) {
  state.viewFlipped = !state.viewFlipped;
  try { localStorage.setItem(VIEW_FLIP_KEY, state.viewFlipped ? "1" : "0"); } catch { /* */ }
  state.view.setHumanWhite(!state.viewFlipped);
}

// Single push of the gated nav state to the UI. Buttons are forced null while
// analyzing or editing (view/goto is rejected in those modes, so the targets
// would be unreachable anyway).
function pushNavToUi(state) {
  const gated = state.analyzing || state.editing;
  setCommentaryNavState(gated ? null : state.commentNavPrev, gated ? null : state.commentNavNext);
}

function showEngineCrashToast() {
  stickyToast(MSG.ENGINE_CRASHED, { variant: "danger" });
}

// Commentary-dock visibility + server-authoritative edit-mode transitions.

function syncCommentsVisibility(state) {
  if (!state.el.dockLeft) return;
  const shouldShow = state.viewing && state.showPgnComments && !isMobileLayout()
    && !state.suppressCommentsForEditTransition;
  const open = isCommentaryOpen();
  if (shouldShow) {
    if (!open) openCommentary();
    setCommentaryText(state.lastViewComment);
    // Re-open rebuilds the body with nav buttons disabled; re-push the
    // still-current targets (they survive a hide -- view-mode state).
    pushNavToUi(state);
  } else if (open) {
    closeCommentary();
  }
}

// Server is authoritative for edit state. We start editing by POSTing
// /game/edit/start; the resulting board_update flips `editing` true, and we
// then enable the client-side board editor extension.
function _onServerEditingStart(state) {
  const seed = _seedFromFen(state.view.getFen());
  state.view.enterEditMode(() => refreshButtons(state), seed);
  state.pendingAnnotation = null;
  pushNavToUi(state);
  refreshButtons(state);
}

function _clearEditTransitionSuppression(state) {
  if (!state.suppressCommentsForEditTransition) return;
  state.suppressCommentsForEditTransition = false;
  syncCommentsVisibility(state);
}

function _onServerEditingStop(state) {
  state.view.exitEditMode();
  _closeEditPopovers(state); // un-float any portaled popover before edit UI hides
  _clearEditTransitionSuppression(state);
  state.pendingAnnotation = null;
  pushNavToUi(state);
  refreshButtons(state);
}

// The viewed game's recents row was force-deleted: the server tore the view
// session down to no-game (close_view) with no board left to publish, so this
// client flips itself to the idle board locally.
function enterIdleAfterViewDelete(state) {
  state.viewing = false;
  state.viewingGameId = null;
  state.view.setGameId(null);
  // Full visual reset: startpos board, empty move list, no arrows --
  // nothing of the deleted game may linger.
  state.view.clearArrows();
  state.view.reset();
  clearUciLog();
  state.movesPlayed = 0;
  _viewingHash = null;
  _viewingSummary = null;
  state.lastViewComment = null;
  state.commentNavPrev = null;
  state.commentNavNext = null;
  state.viewGameOverAlertShown = false;
  state.dismissGameOverToast?.();
  state.dismissGameOverToast = null;
  setAnalyzing(state, false);
  closeAi();
  pushNavToUi(state);
  syncCommentsVisibility(state);
  restoreDebugWindows(state.ctx.events);
  resetXgame(state);
  refreshXgameToasts(state);
  state.resignAvailable = false;
  state.gameOver = false;
  _playInProgress = false;
  state.el.boardHost.classList.add("board-idle");
  setDisabled(state.el.newGameBtn, false);
  showFinishedBadge(state, "");
  refreshButtons(state);
  window.dispatchEvent(new CustomEvent(APP_EVT.VIEWING_CHANGED, {
    detail: { viewing: false },
  }));
}

// A finished game flips into view mode on its recents copy (the live game is
// finalized before game_result fires; the server flushes recents first),
// landing on the final position. Zero-move games never reach recents -- skip.
async function _enterViewOnGameOver(state, gameId) {
  if (state.viewing || state.editing || !gameId || !state.movesPlayed) return;
  state.autoViewFromGameOver = true;
  const ok = await openXgameTarget(state, gameId, { landAtPly: state.movesPlayed });
  if (!ok) state.autoViewFromGameOver = false;
}

async function _enterEditFromCurrentMode(state) {
  // Server requires view mode before edit. From play mode, flip into
  // view via /game/view/start (no recents write); /game/import would
  // pollute the recents history with the current play position.
  if (!state.viewing) {
    if (!await _confirmDiscardActiveGame({
      message: MSG.CONFIRM_EDIT_FROM_PLAY,
      okLabel: MSG.EDIT_POSITION,
    })) return;
    // Suppress the commentary dock for the duration of the transient
    // play->view->edit flip. Without this, syncCommentsVisibility
    // races view_last() and resets the cursor to 0.
    state.suppressCommentsForEditTransition = true;
    try {
      closeAi();
      const r = await state.ctx.api("POST", "/game/view/start", {});
      state.view.setGameId(r.game_id);
      await state.ctx.api("POST", "/game/sync", {});
    } catch (e) {
      _clearEditTransitionSuppression(state);
      reportError(state.ctx, MSG.EDIT_POSITION_FAILED, e);
      return;
    }
  }
  if (state.analyzing) {
    const ok = await confirm({
      message: MSG.CONFIRM_EDIT_STOP_ANALYSIS,
      okLabel: MSG.EDIT_POSITION,
      cancelLabel: MSG.KEEP_ANALYZING,
      destructive: true,
    });
    if (!ok) {
      _clearEditTransitionSuppression(state);
      return;
    }
  }
  try {
    closeAi();
    await state.ctx.api("POST", "/game/edit/start", {});
  } catch (e) {
    _clearEditTransitionSuppression(state);
    reportError(state.ctx, MSG.EDIT_POSITION_FAILED, e);
  }
}

// Control-bar state from server events (board state is GameView's job).
function handleBusEvent(state, ai, aiCtx, evt) {
  // AI events: buffer until replay completes, then dedupe by seq.
  if (evt.kind?.startsWith(AI_KIND_PREFIX)) {
    if (ai.rehydrating) ai.liveBuffer.push(evt);
    else if (shouldAdoptAiSession(state, evt)) adoptRemoteAiSession(state, ai, aiCtx, evt);
    else dispatchAiEventOrdered(ai, aiCtx, evt);
    return;
  }
  switch (evt.kind) {
    case KIND.ENGINE_SEARCH_START: {
      // Engine busy during an in-flight AI turn = agent tool call; flip
      // the status so the user sees what's taking time. Gated on the turn,
      // not just the open panel: game-move searches must not touch it.
      if (isAiOpen() && aiTurnInFlight(state)) setAiStatus("engine");
      break;
    }
    case KIND.ENGINE_INFO: {
      // Engine produced an info chunk -- search is delivering. Drop
      // the "engine searching" hint back to "waiting" so the user
      // knows the agent will narrate next.
      if (isAiOpen() && aiTurnInFlight(state)) setAiStatus("waiting");
      break;
    }
    case KIND.BOARD_UPDATE: {
      _cachedBoardUpdate = evt;
      state.movesPlayed = evt.payload.moves_san?.length ?? 0;
      state.gameOver = false;
      showFinishedBadge(state, "");
      // Server-authoritative edit state. Transitions drive the client
      // editor extension on/off; the ribbon UI follows `editing`.
      const wasEditing = state.editing;
      state.editing = !!evt.payload.editing;
      if (state.editing && !wasEditing) _onServerEditingStart(state);
      else if (!state.editing && wasEditing) _onServerEditingStop(state);
      // View mode swaps the ribbon and suppresses play-mode signals
      // (resignAvailable, etc.) — the user isn't playing yet.
      const v = evt.payload.view;
      const wasViewing = state.viewing;
      const prevGameId = state.viewingGameId;
      state.viewingGameId = evt.game_id ?? null;
      const gameChanged = state.viewingGameId !== prevGameId;
      // New game (New Game / Play from here / opening a different game):
      // drop the prior game's UCI traffic before its own lands.
      if (prevGameId != null && gameChanged) clearUciLog();
      state.viewing = !!v;
      // Read analyzing early: syncCommentsVisibility (called below) gates
      // view/goto on !analyzing; the main analyzing block runs later in
      // the same event but would be too late.
      if (typeof evt.payload.analyzing === "boolean") setAnalyzing(state, evt.payload.analyzing);
      if (state.viewing) {
        // Auto-entry from game over: set just before the /game/import round
        // trip that produced this event; consumed on arrival.
        const autoEntry = !wasViewing && state.autoViewFromGameOver;
        state.autoViewFromGameOver = false;
        if (!wasViewing || gameChanged) {
          // The play-mode dialog already announced the result; start the
          // view session with the game-over toast spent.
          state.viewGameOverAlertShown = autoEntry;
          state.dismissGameOverToast?.();
          state.dismissGameOverToast = null;
          // Game switched: clear stale x-game state + close live toasts BEFORE
          // the in-band refreshXgameToasts (so it can't fire on prior-game data).
          // fetchXgameInfo then repopulates and re-renders.
          resetXgame(state);
          fetchXgameInfo(state, state.viewingGameId);
          // New view session: require visiting an earlier ply before the
          // last-ply auto-resume can fire (the landing event itself must not
          // self-trigger).
          state.viewReachedNonLast = false;
        }
        state.viewCursor = v.cursor ?? 0;
        state.viewTotalPlies = v.total_plies ?? 0;
        state.viewGameOver = !!v.game_over;
        // termination is stamped at EVERY cursor of a finished game.
        // ANDing game_over narrows to the position with a real outcome:
        // mid-game cursors of finished games report game_over false.
        state.viewPositionTerminal =
          state.viewGameOver && FORCED_TERMINATIONS.has(v.termination);
        // Auto-return to the SAME play game (no fork) when a resumable
        // session (entered via /view/start on the live game) scrubs to the
        // last ply. The viewReachedNonLast gate (set only when cursor was
        // earlier than the end) keeps the landing event from self-firing.
        if (state.viewCursor < state.viewTotalPlies) state.viewReachedNonLast = true;
        if (v.resumable && state.viewReachedNonLast
            && state.viewCursor === state.viewTotalPlies) {
          resumeLivePlay(state);
        }
        state.lastViewComment = v.comment ?? null;
        _viewingHash = v.view_hash ?? null;
        _viewingSummary = v.view_summary ?? null;
        // Single source of truth for commentary navigation. The server
        // ships fresh prev/next with every view payload, so game
        // switches (import while open) can't leave stale plies behind.
        state.commentNavPrev = v.prev_comment ?? null;
        state.commentNavNext = v.next_comment ?? null;
        pushNavToUi(state);
        syncCommentsVisibility(state);
        if (v.result) showFinishedBadge(state, resultBadge(v.result));
        if (state.viewGameOver && state.viewCursor === state.viewTotalPlies && v.result && !state.viewGameOverAlertShown) {
          state.viewGameOverAlertShown = true;
          const node = document.createElement("span");
          node.className = "toast-sort-msg";
          const msg = document.createElement("span");
          msg.className = "toast-grow";
          msg.textContent = formatViewGameOver(v);
          node.append(msg, makeToastDismissBtn(() => { state.dismissGameOverToast?.(); state.dismissGameOverToast = null; }));
          state.dismissGameOverToast = toast(node, { variant: "neutral", duration: 6000 });
        }
        state.resignAvailable = false;
        // Board is read-only in view mode; the user navigates via ribbon.
        state.view.setEnabled(false);
        // On entry (incl. /game/sync remount, wasViewing false): resumable
        // takes the player's POV from the payload color -- it survives
        // remount, unlike state.humanWhite. Both resumable-without-color and
        // imported fall back to the flip preference.
        if (!wasViewing) {
          if (typeof v.resume_human_white === "boolean") {
            state.humanWhite = v.resume_human_white;
            state.view.setHumanWhite(state.humanWhite);
          } else if (autoEntry) {
            // Keep the POV the game was just played from -- the game-over
            // flip into view must not flip the board.
            state.view.setHumanWhite(state.humanWhite);
          } else {
            state.view.setHumanWhite(!state.viewFlipped);
          }
        }
      } else {
        state.viewPositionTerminal = false;
        state.lastViewComment = null;
        _viewingHash = null;
        _viewingSummary = null;
        state.commentNavPrev = null;
        state.commentNavNext = null;
        pushNavToUi(state);
        syncCommentsVisibility(state);
        if (wasViewing) restoreDebugWindows(state.ctx.events);
        // Leaving view mode -- x-game state is per-viewed-game; drop it.
        if (wasViewing) resetXgame(state);
        state.resignAvailable = true;
      }
      // Cursor or viewing state may have just changed -- re-evaluate
      // the parent / children toasts. Open/close as needed.
      refreshXgameToasts(state);
      // Notify the perspective router so the nav label can swap
      // Play <-> View when the mode flips.
      if (wasViewing !== state.viewing) {
        window.dispatchEvent(new CustomEvent(APP_EVT.VIEWING_CHANGED, {
          detail: { viewing: state.viewing },
        }));
      }
      const wasHumanToMove = humanToMove(state);
      if (typeof evt.payload.human_white === "boolean") {
        state.humanWhite = evt.payload.human_white;
      }
      if (evt.payload.turn) state.turn = evt.payload.turn;
      const isHumanToMove = humanToMove(state);
      // humanWhite is settled above; rebuild the eval strip in engine POV.
      feedEvalBar(state, evt.payload.eval_history);
      if (typeof evt.payload.analyzing === "boolean") {
        setAnalyzing(state, evt.payload.analyzing);
        syncBoardInputEnabled(state);
        syncPausedUi(state);
        pushNavToUi(state);
        if (!state.analyzing) {
          dismissAnalysisToast(state.aiShared);
        } else {
          // A round just started (here or on another client): any error
          // toast from the last round no longer applies.
          dismissAiErrorToast();
          if (!state.aiShared.dismissAnalysisToast) {
            // Server reports analysis active but no toast exists -- we
            // were re-mounted (e.g. user navigated to another
            // perspective and came back). Restore the toast so the
            // user can still see and dismiss it.
            showAnalysisToastImpl(state);
          }
        }
      }
      // Title the UCI Log with the engine whose traffic it shows. The
      // analysis engine is named at analysis start; here we cover the play
      // engine (or bare when none, e.g. viewing an imported game).
      if (!state.analyzing) setUciLogEngine(evt.payload.engine_name || "");
      // Engine's (non-analysis) turn just started -- drop the prior turn's
      // traffic so the log doesn't carry it into the new one.
      if (!state.analyzing && !state.viewing && wasHumanToMove && !isHumanToMove) clearUciLog();
      state.el.boardHost.classList.remove("board-idle");
      setDisabled(state.el.newGameBtn, false);
      refreshButtons(state);
      _playInProgress = state.movesPlayed > 0 && !state.gameOver && !state.viewing;
      // GameView cleared arrows above (runs before this handler on the same
      // bus); restore the AI recommendation arrow.
      reapplyAiRecommendation(state, evt.payload.fen);
      break;
    }
    case KIND.GAME_RESULT:
      state.gameOver = true;
      state.paused = false;
      setAnalyzing(state, false);
      dismissAnalysisToast(state.aiShared);
      state.resignAvailable = false;
      setDisabled(state.el.newGameBtn, false);
      state.el.boardHost.classList.add("board-idle");
      syncPausedUi(state);
      showFinishedBadge(state, formatResult(evt.payload, state.humanWhite));
      refreshButtons(state);
      _playInProgress = false;
      showAlert({
        message: formatGameOver(evt.payload, state.humanWhite),
        messageClass: "game-over-message",
      });
      _enterViewOnGameOver(state, evt.game_id);
      break;
    case KIND.CLOCK_TICK:
      if (typeof evt.payload.paused === "boolean" && evt.payload.paused !== state.paused) {
        state.paused = evt.payload.paused;
        syncBoardInputEnabled(state);
        syncPausedUi(state);
        refreshButtons(state);
      }
      break;
  }
}

export const playPerspective = {
  id: "play",
  label: "Play",

  async mount(root, ctx) {
    root.innerHTML = PLAY_PERSPECTIVE_HTML;

    // Shared mutable view/x-game state, passed by ref to the extracted
    // x-game helpers so they can read/write the same scalars as mount.
    // `view` is filled in right after mountGameView returns below.
    const state = {
      analyzing: false,
      viewing: false,
      viewCursor: 0,
      viewingGameId: null,
      lastViewNavKind: "precise",
      editing: false,
      showPgnComments: true,
      suppressCommentsForEditTransition: false,
      lastViewComment: null,
      gameTcInitial: null,
      gameTcIncrement: null,
      viewGameOverAlertShown: false,
      autoViewFromGameOver: false,
      dismissGameOverToast: null,
      // Edit-mode staged annotation: null=no change, ""=clear, "text"=set at
      // entry ply. Reset on each edit entry and on /edit/cancel.
      pendingAnnotation: null,
      viewFlipped: false,
      takebackPending: false,
      commentNavPrev: null,
      commentNavNext: null,
      playFromHereInflight: false,
      resumeInflight: false,
      enterViewInflight: false,
      viewReachedNonLast: false,
      // Set while this client drives an analysis stop/restart round trip: its
      // own panel sequencing wins over the events that transition emits.
      analysisTransitionInFlight: false,
      // Set by unmount(); post-await continuations check it so they don't
      // build UI (toasts, dock windows) into a torn-down perspective.
      unmounted: false,
      aiEnabled: false,
      aiTitleModel: "",
      noEngine: false,
      allowTakeback: true,
      movesPlayed: 0,
      viewTotalPlies: 0,
      viewGameOver: false,
      viewPositionTerminal: false,
      humanWhite: true,
      turn: SIDE.WHITE,
      resignAvailable: false,
      gameOver: false,
      paused: false,
      ctx,
      api: ctx.api,
      view: null,
      // Edit-mode DOM refs + refreshButtons, filled in below once they exist.
      el: null,
      refreshButtons: null,
      // turnFinished: AI turn ended naturally (server stays in ANALYSIS, board
      // locked, ribbon stops "stopping"); reset on next analyze start.
      // dismissAnalysisToast: handle to the persistent "Analysis mode" toast.
      aiShared: { turnFinished: false, dismissAnalysisToast: null },
      xgame: {
        gameId: null,
        parentGameId: null,
        parentSummary: null,
        forkPly: null,
        children: [],
        parentToastDismissed: false,
        childrenToastDismissed: false,
        parentToastHandle: null,
        childrenToastHandle: null,
      },
    };

    const boardHost = root.querySelector(".play-board-host");
    const dockLeft = root.querySelector(".play-dock-left");
    const sideHost = root.querySelector(".play-side-host");
    setDockContainer(dockLeft);
    boardHost.classList.add("board-idle");
    const newGameBtn = root.querySelector("#new-game");
    const importBtn = root.querySelector("#import-pos");
    const resignBtn = root.querySelector("#resign");
    const takebackBtn = root.querySelector("#takeback");
    const switchSidesBtn = root.querySelector("#switch-sides");
    const pauseBtn = root.querySelector("#pause");
    const analyzeBtn = root.querySelector("#analyze");
    const uciLogBtn = root.querySelector("#uci-log-btn");
    const pvTableBtn = root.querySelector("#pv-table-btn");
    // View ribbon (shown only while a game is loaded into view mode).
    const playRibbon = root.querySelector("#board-controls");
    const viewRibbon = root.querySelector("#view-controls");
    const viewNewGameBtn = root.querySelector("#view-new-game");
    const viewImportBtn = root.querySelector("#view-import");
    const viewFirstBtn = root.querySelector("#view-first");
    const viewBackBtn = root.querySelector("#view-back");
    const viewForwardBtn = root.querySelector("#view-forward");
    const viewLastBtn = root.querySelector("#view-last");
    const viewFlipBtn = root.querySelector("#view-flip");
    const editFlipBtn = root.querySelector("#edit-flip");
    const editAnnotateBtn = root.querySelector("#edit-annotate");
    const viewAnalyzeBtn = root.querySelector("#view-analyze");
    const viewPlayFromHereBtn = root.querySelector("#view-play-from-here");
    const viewEditBtn = root.querySelector("#view-edit");
    const savePgnBtn = root.querySelector("#save-pgn");
    const viewSavePgnBtn = root.querySelector("#view-save-pgn");
    const editPosBtn = root.querySelector("#edit-pos");
    const editRibbon = root.querySelector("#edit-controls");
    const editSideBtn = root.querySelector("#edit-side");
    const editSidePopover = root.querySelector("#edit-side-popover");
    const editSidePopoverWrap = editSidePopover.parentElement; // restore target after float
    const editSideTogglePill = root.querySelector("#edit-side-toggle");
    const editCastleBtn = root.querySelector("#edit-castle-btn");
    const editCastlePopover = root.querySelector("#edit-castle-popover");
    const editCastlePopoverWrap = editCastlePopover.parentElement;
    const editCastleCb = {
      wK: root.querySelector("#edit-castle-cb-wk"),
      wQ: root.querySelector("#edit-castle-cb-wq"),
      bK: root.querySelector("#edit-castle-cb-bk"),
      bQ: root.querySelector("#edit-castle-cb-bq"),
    };
    const editConfirmBtn = root.querySelector("#edit-confirm");
    const editCancelBtn = root.querySelector("#edit-cancel");
    const noEngineBanner = root.querySelector("#no-engine-banner");
    const noEngineBannerBtn = noEngineBanner.querySelector(".no-engine-banner__btn");
    // DOM refs read by lifted module-level handlers (edit popovers,
    // refreshButtons). Mount keeps the bare consts for listener wiring.
    state.el = {
      editSideBtn, editSidePopover, editSidePopoverWrap,
      editCastleBtn, editCastlePopover, editCastlePopoverWrap,
      editSideTogglePill, editCastleCb, editRibbon, playRibbon, viewRibbon,
      pauseBtn, takebackBtn, savePgnBtn, switchSidesBtn, resignBtn, analyzeBtn,
      viewFirstBtn, viewBackBtn, viewForwardBtn, viewLastBtn, viewSavePgnBtn,
      viewPlayFromHereBtn, viewAnalyzeBtn, boardHost, newGameBtn,
    };

    // Tracks "server has zero engines registered." Drives both the
    // CTA banner and per-button gating (view-analyze, AI settings).
    // Mirrored in JS state so refreshButtons(state) can read it without an
    // extra DOM query each call.
    let buttonsReady = false; // gate setNoEngine's early refreshButtons until state.view is wired
    function setNoEngine(v) {
      state.noEngine = !!v;
      noEngineBanner.classList.toggle("hidden", !state.noEngine);
      if (buttonsReady) refreshButtons(state);
    }
    async function checkEngines() {
      try {
        const r = await ctx.api("GET", "/engines");
        setNoEngine(!r.selected_id);
      } catch {
        // Network/auth failure: leave banner hidden -- a hidden banner
        // is preferable to a spurious one when we can't verify state.
        setNoEngine(false);
      }
    }
    noEngineBannerBtn.addEventListener("click", () => openSettings(SETTINGS_TAB_ENGINES));
    const onEnginesChanged = (e) => {
      setNoEngine(!e.detail?.activeId);
    };
    window.addEventListener(APP_EVT.ENGINES_CHANGED, onEnginesChanged);
    checkEngines();

    // Fetch settings before mount so the board picks up the saved style.
    let initialBoardStyle = null;
    try {
      const s0 = await ctx.api("GET", "/settings");
      initialBoardStyle = s0.board_style || null;
    } catch {
      // ignore — fall back to default style
    }

    // --- GameView: board host on top, side host (moves+engine) below. ---
    const view = mountGameView(boardHost, {
      events: ctx.events,
      interactive: true,
      sideContainer: sideHost,
      boardStyle: initialBoardStyle,
      onMove: async (uci) => {
        // Drop during analysis: exit analysis and play the move. A running
        // session asks first; a finished AI turn exits silently, matching
        // the ribbon's one-click Resume (see onPauseImpl).
        let held = false;
        if (state.analyzing) {
          // Hold piece rendering: stop_analysis republishes the pre-move
          // FEN, which would snap the dropped piece back before the move's
          // own update re-animates it (visible stutter). Release BEFORE
          // snapBack so the sync echo still lands via the normal path.
          view.holdBoard();
          held = true;
          // Snap the optimistically-moved piece back on any bail-out.
          const snapBack = () => ctx.api("POST", "/game/sync", {}).catch(() => {});
          if (!aiAnalysisDone(state)) {
            const ok = await confirm({
              message: MSG.CONFIRM_MOVE_STOP_ANALYSIS,
              okLabel: MSG.PLAY_MOVE,
              cancelLabel: MSG.KEEP_ANALYZING,
              destructive: true,
            });
            if (!ok) {
              view.releaseBoard();
              await snapBack();
              return;
            }
          }
          if (!await stopAnalysisFromUi(state)) {
            view.releaseBoard();
            await snapBack();
            return;
          }
          try {
            await ctx.api("POST", "/game/resume", {});
          } catch (e) {
            reportError(ctx, MSG.RESUME_FAILED, e);
            view.releaseBoard();
            await snapBack();
            return;
          }
        }
        try {
          await ctx.api("POST", "/game/move", { uci });
          if (held) view.releaseBoard();
        } catch (e) {
          reportError(ctx, MSG.MOVE_REJECTED, e);
          // Rejection while held: the server's same-FEN rebroadcast may
          // have been swallowed by the hold, so force the snap-back.
          if (held) view.releaseBoard({ snap: true });
        }
      },
      // Click on a move in the list (view mode only) → jump cursor to
      // the position AFTER that move, i.e. ply = plyIndex + 1.
      onMoveJump: (plyIndex) => doViewNav(state, "/game/view/goto", { ply: plyIndex + 1 }),
      // Click a past move in PLAY mode → flip into server view mode at that
      // ply. Scrubbing to the live game's last ply auto-returns to play.
      onPlayMoveClick: (plyIndex) => enterViewAtPly(state, plyIndex),
      // Fork glyphs. Fresh map per render; both child-here (this game
      // has forks at this ply) and own-fork-ply (this game itself
      // diverged from its parent here) get a glyph.
      forkInfoFn: () => {
        const m = new Map();
        for (const c of state.xgame.children || []) {
          // fork_ply is 1-based; cell index = fork_ply - 1.
          const idx = (c.fork_ply ?? 0) - 1;
          if (idx < 0) continue;
          const cur = m.get(idx) ?? { childCount: 0, isOwnForkPly: false };
          cur.childCount += 1;
          m.set(idx, cur);
        }
        if (state.xgame.parentGameId && state.xgame.forkPly != null) {
          const idx = state.xgame.forkPly - 1;
          if (idx >= 0) {
            const cur = m.get(idx) ?? { childCount: 0, isOwnForkPly: false };
            cur.isOwnForkPly = true;
            m.set(idx, cur);
          }
        }
        return m.size > 0 ? m : null;
      },
      // Glyph click: nav to the ply AND reset both per-game don't-nag
      // flags (live + persisted) so the toasts re-fire at this ply.
      // Skip the server round-trip when we're already there.
      onForkClick: (plyIndex) => {
        if (state.analyzing) return;
        state.xgame.parentToastDismissed = false;
        state.xgame.childrenToastDismissed = false;
        _setXgameDismissed(state.xgame.gameId, "parent", false);
        _setXgameDismissed(state.xgame.gameId, "children", false);
        if (state.viewCursor === plyIndex + 1) {
          refreshXgameToasts(state);
          return;
        }
        doViewNav(state, "/game/view/goto", { ply: plyIndex + 1 });
      },
    });
    state.view = view;

    // Rail dock: capacity-one dock destination in the band under the moves
    // list (positioned by positionSideRail). Default home of the Engine Eval
    // window; any dock window can be dragged into it while it's free.
    // Bar click -> enter view mode at that ply (same as clicking the move in
    // the list); handlers go through setEvalBarCallbacks because the window
    // body outlives this mount's closures.
    const railDock = document.createElement("div");
    railDock.className = "play-rail-dock dock-empty";
    const sideRail = sideHost.querySelector(".game-view-side");
    const movesSection = sideRail?.querySelector(".game-view-moves");
    if (movesSection) movesSection.after(railDock);
    else sideRail?.appendChild(railDock);
    setRailDockContainer(railDock);
    setEvalBarCallbacks({
      onBarClick: (ply) => enterViewAtPly(state, ply),
      isBarNavigable: (ply) => canEnterViewAtPly(state, ply),
    });
    // X on the eval graph (dock slot or float) -> clear setting.
    evalBar.setOnUserClose(() => putSettingOff(ctx, SETTING_SHOW_EVAL_GRAPH));

    state.el.dockLeft = dockLeft;
    setAiInlineHost(root.querySelector(".play-ai-inline"));
    // X on the commentary window (dock slot or float) -> clear setting.
    commentaryWindow.setOnUserClose(() => {
      state.showPgnComments = false;
      putSettingOff(ctx, SETTING_SHOW_PGN_COMMENTS);
    });
    const onCommentsResize = () => { syncCommentsVisibility(state); };
    window.addEventListener("resize", onCommentsResize);

    // Closing the AI window mid-turn = same effect as clicking toolbar Stop:
    // snapshot view state, stop analysis, close all dock panels.
    setOnUserCloseAi(() => { stopAnalysisFromUi(state); });
    await refreshSettings(state);
    const onSettingsChanged = () => {
      refreshSettings(state, { notifyOnDrift: true }).then(() => refreshButtons(state));
    };
    window.addEventListener(APP_EVT.SETTINGS_CHANGED, onSettingsChanged);

    // sturddle:layout-changed fires when ribbon_float toggled in main.js or
    // when the user closes the ribbon WinBox. Re-run refreshButtons so the
    // active ribbon is mounted in the WinBox (or unhidden from the DOM).
    const onLayoutChanged = () => { refreshButtons(state); };
    window.addEventListener(APP_EVT.LAYOUT_CHANGED, onLayoutChanged);

    // sturddle:recents-changed fires when another perspective (e.g. the
    // import dialog) mutated the recents store. Re-fetch x-game info
    // for the currently-viewed game so the fork glyph + banner reflect
    // the new state (B3: glyph stale after a child was deleted).
    const onRecentsChanged = (ev) => {
      // The currently-viewed game was force-deleted -> idle board.
      if (ev.detail?.deletedGameId && state.viewing && !state.editing
          && ev.detail.deletedGameId === state.viewingGameId) {
        enterIdleAfterViewDelete(state);
        return;
      }
      if (state.viewing && state.viewingGameId) fetchXgameInfo(state, state.viewingGameId);
    };
    window.addEventListener(APP_EVT.RECENTS_CHANGED, onRecentsChanged);

    const pausedBadge = document.getElementById("paused-badge");
    const finishedBadge = document.getElementById("finished-badge");
    state.el.pausedBadge = pausedBadge;
    state.el.finishedBadge = finishedBadge;

    // Resign is enabled whenever there is an active game; cleared on
    // game_result. We track it explicitly so paused-state can additionally
    // gate it without losing the "active game" signal.
    buttonsReady = true;
    state.refreshButtons = () => refreshButtons(state);

    // Hydrate the persisted flip preference (see onViewFlipImpl for rationale).
    try { state.viewFlipped = localStorage.getItem(VIEW_FLIP_KEY) === "1"; } catch { /* */ }

    // Private replay-buffer state + the deps the AI dispatch needs.
    const ai = { rehydrating: true, liveBuffer: [], maxSeq: 0 };
    const aiCtx = { view, api: ctx.api, refreshButtons: () => refreshButtons(state), aiShared: state.aiShared };
    rehydrateAiPanel(ai, aiCtx);

    // --- Hook events for control-bar state changes (board state changes
    //     are GameView's responsibility). ---
    // Registered last: handleBusEvent + its lifted helpers read state.el.* and
    // state.view with no null guards, so all of those must be populated above.
    const offEvent = state.ctx.events.on((evt) => handleBusEvent(state, ai, aiCtx, evt));

    // Render the cached board_update straight to the renderer, NOT via the bus:
    // the bus handler's side effects (syncCommentsVisibility -> /view/goto) would
    // POST stale cursor data if an external import swapped games while unmounted.
    // The /sync POST above still overrides with fresh server state.
    if (_cachedBoardUpdate) {
      view.applyEvent(_cachedBoardUpdate);
    }

    const onNewGame = () => onNewGameImpl(state);
    const onResign = () => onResignImpl(state);
    const onSavePgn = () => onSavePgnImpl(state);
    const onTakeback = () => onTakebackImpl(state);
    const onImport = () => onImportImpl(state);
    const onSwitchSides = () => onSwitchSidesImpl(state);
    const onPause = () => onPauseImpl(state);

    const onEditPosition = () => _enterEditFromCurrentMode(state);
    const onViewEditPosition = () => _enterEditFromCurrentMode(state);
    const onEditSide = (ev) => editSidePopoverToggle(state, ev);
    const onEditSideToggle = () => editSideFlip(state);
    const onEditCastleCb = (right) => () => editCastleToggle(state, right);
    const onEditCastleBtn = (ev) => editCastlePopoverToggle(state, ev);
    const onDocClickClosePopover = (ev) => editDocClickClose(state, ev);
    const onEditAnnotate = () => onEditAnnotateImpl(state);
    const onEditConfirm = () => onEditConfirmImpl(state);
    const onEditCancel = () => onEditCancelImpl(state);
    const onViewNav = (endpoint) => () => doViewNav(state, endpoint);
    const onViewFlip = () => onViewFlipImpl(state);
    const onViewFirst = onViewNav("/game/view/first");
    const onViewBack = onViewNav("/game/view/back");
    const onViewForward = onViewNav("/game/view/forward");
    const onViewLast = onViewNav("/game/view/last");

    setCommentaryNavHandlers(
      () => { if (state.commentNavPrev != null) doViewNav(state, "/game/view/goto", { ply: state.commentNavPrev }); },
      () => { if (state.commentNavNext != null) doViewNav(state, "/game/view/goto", { ply: state.commentNavNext }); },
    );

    const onPlayFromHere = () => onPlayFromHereImpl(state);
    const showAnalysisToast = () => showAnalysisToastImpl(state);
    const onAnalyze = () => onAnalyzeImpl(state);
    const onReanalyze = () => onReanalyzeImpl(state);
    setOnReanalyzeAi(onReanalyze);

    // Cmd/Ctrl+O opens the import dialog. Skip when typing in an input or
    // when a dialog is already open, so it doesn't clobber an in-progress
    // form.
    const onKeydown = (ev) => {
      if (!(ev.key === "o" || ev.key === "O")) return;
      if (!(ev.metaKey || ev.ctrlKey)) return;
      const t = ev.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (document.querySelector("wa-dialog[open]")) return;
      ev.preventDefault();
      onImport();
    };
    window.addEventListener("keydown", onKeydown);

    newGameBtn.addEventListener("click", onNewGame);
    importBtn.addEventListener("click", onImport);
    editPosBtn?.addEventListener("click", onEditPosition);
    resignBtn.addEventListener("click", onResign);
    const onUciLog = () => toggleUciLogWindow(ctx.events);
    const onPvTable = () => togglePvTableWindow(ctx.events);
    uciLogBtn?.addEventListener("click", onUciLog);
    pvTableBtn?.addEventListener("click", onPvTable);
    restoreDebugWindows(ctx.events);
    // After restoreDebugWindows: sync makes the server re-emit board state and
    // the last engine_info, and only a panel that already exists can catch it.
    // Reload would otherwise leave Search Lines blank until the next info line.
    ctx.api("POST", "/game/sync", {}).catch(() => {});
    takebackBtn.addEventListener("click", onTakeback);
    switchSidesBtn.addEventListener("click", onSwitchSides);
    pauseBtn.addEventListener("click", onPause);
    pausedBadge?.addEventListener("click", onPause);
    const onPausedBoardClick = () => hintResumeFromPausedBoard(state);
    boardHost.addEventListener("click", onPausedBoardClick);
    analyzeBtn.addEventListener("click", onAnalyze);
    viewNewGameBtn.addEventListener("click", onNewGame);
    viewImportBtn.addEventListener("click", onImport);
    viewFirstBtn.addEventListener("click", onViewFirst);
    viewBackBtn.addEventListener("click", onViewBack);
    viewForwardBtn.addEventListener("click", onViewForward);
    viewLastBtn.addEventListener("click", onViewLast);
    viewFlipBtn.addEventListener("click", onViewFlip);
    editFlipBtn.addEventListener("click", onViewFlip);
    viewAnalyzeBtn.addEventListener("click", onAnalyze);
    viewEditBtn.addEventListener("click", onViewEditPosition);
    savePgnBtn?.addEventListener("click", onSavePgn);
    viewSavePgnBtn?.addEventListener("click", onSavePgn);
    viewPlayFromHereBtn.addEventListener("click", onPlayFromHere);
    editSideBtn.addEventListener("click", onEditSide);
    editSideTogglePill.addEventListener("click", onEditSideToggle);
    editCastleBtn.addEventListener("click", onEditCastleBtn);
    const onCastleWK = onEditCastleCb("wK");
    const onCastleWQ = onEditCastleCb("wQ");
    const onCastleBK = onEditCastleCb("bK");
    const onCastleBQ = onEditCastleCb("bQ");
    editCastleCb.wK.addEventListener("click", onCastleWK);
    editCastleCb.wQ.addEventListener("click", onCastleWQ);
    editCastleCb.bK.addEventListener("click", onCastleBK);
    editCastleCb.bQ.addEventListener("click", onCastleBQ);
    document.addEventListener("click", onDocClickClosePopover);
    const onPopoverDismiss = () => _closeEditPopovers(state);
    window.addEventListener("resize", onPopoverDismiss);
    window.addEventListener(APP_EVT.RIBBON_MOVED, onPopoverDismiss);
    // Closing the float WinBox re-docks the ribbon; un-float and close any
    // open popover so it doesn't orphan at <body>.
    window.addEventListener(APP_EVT.RIBBON_FLOAT_CLOSED, onPopoverDismiss);
    editAnnotateBtn.addEventListener("click", onEditAnnotate);
    editConfirmBtn.addEventListener("click", onEditConfirm);
    editCancelBtn.addEventListener("click", onEditCancel);

    const offCrash = ctx.events.on(async (evt) => {
      if (evt.kind !== "system") return;
      const err = evt.payload?.error;
      if (err === "engine_terminated") {
        view.clearArrows();
        if (state.analyzing) {
          await onAnalyze();
        }
        showEngineCrashToast();
      } else if (err === "analysis_engine_failed") {
        // Server already reverted out of ANALYZING (board_update); just say why.
        view.clearArrows();
        const detail = (evt.payload?.detail || "").slice(0, MAX_TOAST_DETAIL);
        const text = detail
          ? `${MSG.ANALYSIS_ENGINE_FAILED} ${detail}`
          : MSG.ANALYSIS_ENGINE_FAILED;
        toast(text, { variant: "danger" });
      } else if (err === "difficulty_unavailable") {
        // Server notifies on every degraded move; one toast at a time.
        if (!_difficultyToastUp) {
          _difficultyToastUp = true;
          const engineName = evt.payload?.engine || MSG.DIFFICULTY_GENERIC_ENGINE;
          let dismiss;
          const actions = [{
            icon: DETAILS_ICON,
            ariaLabel: MSG.DIFFICULTY_DETAILS_ARIA,
            onClick: async () => {
              await showAlert({
                message: (resolve) => buildDifficultyDetails(engineName, resolve),
                width: DETAILS_DIALOG_WIDTH,
              });
              dismiss?.();
            },
          }];
          dismiss = stickyToast(MSG.DIFFICULTY_UNAVAILABLE, {
            variant: "warning",
            actions,
            onDismiss: () => { _difficultyToastUp = false; },
          });
        }
      }
    });

    return {
      ready: Promise.resolve(),
      async canUnmount() {
        if (!state.editing) return true;
        return await confirm({
          message: MSG.CONFIRM_LEAVE_EDIT,
          okLabel: MSG.LEAVE,
          cancelLabel: MSG.STAY,
          destructive: true,
        });
      },
      unmount() {
        state.unmounted = true;
        // Un-float/close edit popovers first: a floated popover lives at
        // <body>, and the teardown below removes the dismiss listeners
        // without firing them -- so it would orphan otherwise.
        _closeEditPopovers(state);
        if (state.editing) {
          // Fire-and-forget cancel so the server doesn't stay stuck in
          // edit mode if the user navigates away.
          ctx.api("POST", "/game/edit/cancel", {}).catch(() => {});
          view.exitEditMode();
        }
        closeDebugWindows();
        // Announce no active ribbon so the global float manager unmounts it.
        window.dispatchEvent(new CustomEvent(APP_EVT.RIBBON_ACTIVE, { detail: { el: null } }));
        // Before setDockContainer(null): commentary now lives in the shared
        // dock, whose teardown would drop its slot out from under it.
        closeCommentary();
        setDockContainer(null);
        setRailDockContainer(null);
        setEvalBarCallbacks(null);
        evalBar.setOnUserClose(null);
        commentaryWindow.setOnUserClose(null);
        closeAi();
        setAiInlineHost(null);
        setOnUserCloseAi(null);
        setOnReanalyzeAi(null);
        dismissAnalysisToast(state.aiShared);
        state.dismissGameOverToast?.();
        state.dismissGameOverToast = null;
        pausedBadge?.classList.add("hidden");
        showFinishedBadge(state, "");
        offCrash();
        offEvent();
        view.unmount();
        // Close any live x-game toasts so they don't outlive the
        // perspective. Plain close (not via the X handler), so the
        // dismiss flags are NOT set -- if the user returns to the
        // play perspective at the same fork ply, the toast re-fires.
        closeXgameToasts(state);
        // Lock class lives on <body>; clear it so it can't outlive the
        // perspective if we unmount mid-analysis.
        document.body.classList.remove(XGAME_LOCK_CLASS);
        window.removeEventListener(APP_EVT.SETTINGS_CHANGED, onSettingsChanged);
        window.removeEventListener(APP_EVT.LAYOUT_CHANGED, onLayoutChanged);
        window.removeEventListener(APP_EVT.ENGINES_CHANGED, onEnginesChanged);
        window.removeEventListener(APP_EVT.RECENTS_CHANGED, onRecentsChanged);
        window.removeEventListener("resize", onCommentsResize);
        window.removeEventListener("keydown", onKeydown);
        newGameBtn.removeEventListener("click", onNewGame);
        importBtn.removeEventListener("click", onImport);
        resignBtn.removeEventListener("click", onResign);
        uciLogBtn?.removeEventListener("click", onUciLog);
        pvTableBtn?.removeEventListener("click", onPvTable);
        takebackBtn.removeEventListener("click", onTakeback);
        switchSidesBtn.removeEventListener("click", onSwitchSides);
        pauseBtn.removeEventListener("click", onPause);
        boardHost.removeEventListener("click", onPausedBoardClick);
        analyzeBtn.removeEventListener("click", onAnalyze);
        viewNewGameBtn.removeEventListener("click", onNewGame);
        viewImportBtn.removeEventListener("click", onImport);
        viewFirstBtn.removeEventListener("click", onViewFirst);
        viewBackBtn.removeEventListener("click", onViewBack);
        viewForwardBtn.removeEventListener("click", onViewForward);
        viewLastBtn.removeEventListener("click", onViewLast);
        viewFlipBtn.removeEventListener("click", onViewFlip);
        editFlipBtn.removeEventListener("click", onViewFlip);
        editAnnotateBtn.removeEventListener("click", onEditAnnotate);
        viewAnalyzeBtn.removeEventListener("click", onAnalyze);
        viewEditBtn.removeEventListener("click", onViewEditPosition);
        viewPlayFromHereBtn.removeEventListener("click", onPlayFromHere);
        editPosBtn?.removeEventListener("click", onEditPosition);
        editSideBtn.removeEventListener("click", onEditSide);
        editSideTogglePill.removeEventListener("click", onEditSideToggle);
        editCastleBtn.removeEventListener("click", onEditCastleBtn);
        editCastleCb.wK.removeEventListener("click", onCastleWK);
        editCastleCb.wQ.removeEventListener("click", onCastleWQ);
        editCastleCb.bK.removeEventListener("click", onCastleBK);
        editCastleCb.bQ.removeEventListener("click", onCastleBQ);
        document.removeEventListener("click", onDocClickClosePopover);
        window.removeEventListener("resize", onPopoverDismiss);
        window.removeEventListener(APP_EVT.RIBBON_MOVED, onPopoverDismiss);
        window.removeEventListener(APP_EVT.RIBBON_FLOAT_CLOSED, onPopoverDismiss);
        editConfirmBtn.removeEventListener("click", onEditConfirm);
        editCancelBtn.removeEventListener("click", onEditCancel);
      },
    };
  },
};
