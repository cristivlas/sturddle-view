// Play perspective: human vs engine.
// Composes the reusable GameView with a game-control bar (New / Take back /
// Resign).

import { mountGameView } from "../game-view.js";
import { APP_EVT } from "../app-events.js";
import { STORAGE_KEY } from "../storage-keys.js";
import { alert as showAlert, confirm, makeToastDismissBtn, openSettings, reportError, toast } from "../dialogs.js";
import { showImportPositionDialog, confirmReplaceViewedGame, confirmDiscardViewedGame } from "../import-position-dialog.js";
import { toggleUciLogWindow, togglePvTableWindow, closeDebugWindows, closeAnalysisOpenedWindows, restoreDebugWindows, snapshotViewAnalysisState, restoreViewAnalysisWindows, setDockContainer, isMobileLayout } from "../play-dock-windows.js";
import {
  setCommentaryDockContainer,
  setOnUserCloseCommentary,
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
  noteAiPosition,
  markAiDone,
  setAiStatus,
  setAiTitle,
  setOnUserCloseAi,
  setOnReanalyzeAi,
  setAiInlineHost,
  isAiOpen,
} from "../play-ai-window.js";
import { terminationLabel } from "../format-termination.js";
import { editAnnotation } from "../annotation-dialog.js";
import { getConfiguredPlayerName } from "../settings-dialog.js";

// Tool name the AI uses to inspect hypothetical positions; the live
// board mirrors `input.fen` while a call with this name is in flight.
const ANALYZE_TOOL_NAME = "analyze";

// Canonical chess result strings as reported by the server.
const RESULT = { WHITE_WIN: "1-0", BLACK_WIN: "0-1", DRAW: "1/2-1/2" };

// Module-scope mirror of "user has a live human-vs-engine game running"
// so other modules (e.g. tournament Replay button) can decide whether
// to confirm before discarding it. Updated from the perspective's
// board_update / game_result handlers below.
let _playInProgress = false;
export function isPlayInProgress() {
  return _playInProgress;
}

// Mirrors the server-reported `analyzing` flag (board_update.analyzing) so
// other modules can decide whether to warn before discarding analysis work.
let _analyzing = false;
export function isAnalyzing() {
  return _analyzing;
}

// SHA-256 hash and summary of the game currently in view (null when in
// play mode). Used by the tournament replay path to skip confirmation
// when the game being replayed is already loaded.
let _viewingHash = null;
let _viewingSummary = null;
let _viewing = false;
export function isViewing() { return _viewing; }
export function getViewingHash() { return _viewingHash; }
export function getViewingSummary() { return _viewingSummary; }

// Last board_update seen by this perspective. Survives unmount so the
// next mount can render the cached state synchronously and resolve
// view.ready before /sync round-trips. The /sync response then
// overrides if anything changed server-side.
let _cachedBoardUpdate = null;

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
    return loser === "white" ? RESULT.BLACK_WIN : RESULT.WHITE_WIN;
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
    const humanLost = (loser === "white") === humanWhite;
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
  // Confirm dialogs.
  CONFIRM_NEW_GAME: "Cancel the game in progress and start a new one?",
  CONFIRM_RESIGN: "Resign the current game?",
  CONFIRM_IMPORT: "Cancel the current game and import another?",
  CONFIRM_EDIT_FROM_PLAY: "Cancel the game in progress and edit the position?",
  CONFIRM_EDIT_STOP_ANALYSIS: "Stop analysis and edit the position?",
  CONFIRM_LEAVE_EDIT: "Leaving will cancel your position edit. Continue?",
  KEEP_PLAYING: "Keep playing",
  KEEP_ANALYZING: "Keep analyzing",
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
  const stm = parts[1] === "b" ? "b" : "w";
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
    case "ai_info": {
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
        });
        if (p.error) {
          toast(p.error_detail || p.error, {
            variant: "danger",
            duration: 6000,
          });
        }
        // Natural completion: hide the "stopping" affordances --
        // toast and ribbon active look. Server stays in ANALYSIS;
        // closing the AI window is what exits. Skipped on
        // cancelled/error to preserve normal cleanup behavior.
        // Per-turn dismissal is correct because each Analyze click
        // is a one-shot turn (no rolling session; see
        // ai-analysis-spec.md §Live session model -- indefinitely
        // postponed).
        if (!p.cancelled && !p.error) {
          aiShared.turnFinished = true;
          aiShared.dismissAnalysisToast?.();
          aiShared.dismissAnalysisToast = null;
          refreshButtons();
        }
      }
      return true;
    }
    case "ai_thinking": {
      const p = evt.payload || {};
      if (typeof p.delta === "string") appendAiThinking(p.delta, p.round ?? 0);
      else if (Number.isFinite(p.thinking_ms)) freezeAiThinking(p.round ?? 0, p.thinking_ms);
      return true;
    }
    case "ai_tool_call": {
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
    case "ai_tool_call_failed": {
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
    case "ai_tool_call_complete": {
      const p = evt.payload || {};
      if (p.name === ANALYZE_TOOL_NAME) view.restorePosition({ animate: false });
      view.clearArrows();
      view.clearEngineInfo();
      return true;
    }
    case "ai_position_note": {
      const p = evt.payload || {};
      noteAiPosition({ round: p.round ?? 0, surfaces: p.surfaces || [] });
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
  if (seq === 1) ai.maxSeq = 0;
  else if (seq && seq <= ai.maxSeq) return;
  if (seq) ai.maxSeq = seq;
  dispatchAiEvent(aiCtx, evt);
}

// Buffer live AI events while the replay GET is in flight, then drain in seq
// order with dedupe. Avoids the GET-then-subscribe race: live events that fire
// between subscribe and replay arrival are held instead of dispatched
// out-of-order.
async function rehydrateAiPanel(ai, aiCtx) {
  try {
    const r = await aiCtx.api("GET", "/game/analysis/replay");
    const events = Array.isArray(r?.events) ? r.events : [];
    if (events.length > 0) {
      openAi();
      resetAi();
      for (const evt of events) dispatchAiEventOrdered(ai, aiCtx, evt);
    }
  } catch { /* */ } finally {
    ai.rehydrating = false;
    const buffered = ai.liveBuffer;
    ai.liveBuffer = [];
    for (const evt of buffered) dispatchAiEventOrdered(ai, aiCtx, evt);
  }
}

const PLAY_PERSPECTIVE_HTML = `
  <section id="play-perspective">
    <div class="play-grid">
      <div class="play-dock-left"></div>
      <aside class="play-comments-host dock-empty" aria-label="PGN commentary"></aside>
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
    // without waiting for the next board_update.
    if (_cachedBoardUpdate) state.view.applyEvent(_cachedBoardUpdate);
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
  // Fetch the target's text from recents, then drive a normal
  // import (server-side enter_view_mode swap). Mirrors the path
  // used by the import dialog's "select a recent" affordance.
  // ``landAtPly``: optional cursor ply to navigate to after the
  // import lands; used for both directions (land at fork_ply --
  // see x-game-navigation.md, Q1/Q2 revised 2026-05-22).
  const landAtPly = opts.landAtPly ?? null;
  // Short-circuit: already viewing this game at the target ply.
  // A re-import would re-animate cm-chessboard to the same
  // position (visible flicker for the user).
  if (
    state.viewing
    && state.viewingGameId === gameId
    && (landAtPly === null || state.viewCursor === landAtPly)
  ) {
    return;
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
  } catch (e) {
    reportError(state.ctx, MSG.OPEN_GAME_FAILED, e);
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
// module-scope mirror (_analyzing) used by isAnalyzing() stays current.
// Direct `state.analyzing = ...` writes will drift -- always call setAnalyzing.
function setAnalyzing(state, v) {
  state.analyzing = !!v;
  _analyzing = state.analyzing;
  // Server flipped out of ANALYSIS -- clear the AI-finished latch
  // so the ribbon can re-enable when the game is paused again.
  if (!state.analyzing) state.aiShared.turnFinished = false;
  document.body.classList.toggle(XGAME_LOCK_CLASS, state.analyzing);
}

// Stop side of the analyze toggle, shared so the AI-window close handler can
// trigger the same flow (snapshot + endpoint + toast + panels) as the toolbar
// Stop button.
async function stopAnalysisFromUi(state) {
  if (!state.analyzing) return;
  snapshotViewAnalysisState();
  try {
    await state.ctx.api("POST", "/game/analysis/stop", {});
  } catch (e) {
    reportError(state.ctx, MSG.STOP_ANALYSIS_FAILED, e);
    return;
  }
  state.aiShared.turnFinished = false;
  state.aiShared.dismissAnalysisToast?.();
  state.aiShared.dismissAnalysisToast = null;
  // The AI window's lifecycle is tied to the analysis session, so it
  // always closes on stop. PV/UCI close only if analysis opened them.
  if (isAiOpen()) closeAi();
  closeAnalysisOpenedWindows();
}

// Edit-mode side/castling popover handlers. Operate on the shared `state`
// (state.el DOM refs, state.view, state.refreshButtons, state.editing).
function _closeSidePopover(state) {
  state.el.editSidePopover.classList.add("hidden");
  state.el.editSideBtn.setAttribute("aria-expanded", "false");
}
function _closeCastlePopover(state) {
  state.el.editCastlePopover.classList.add("hidden");
  state.el.editCastleBtn.setAttribute("aria-expanded", "false");
}
function editSidePopoverToggle(state, ev) {
  ev.stopPropagation();
  if (!state.el.editSidePopover.classList.contains("hidden")) {
    _closeSidePopover(state);
  } else {
    state.el.editSidePopover.classList.remove("hidden");
    state.el.editSideBtn.setAttribute("aria-expanded", "true");
  }
}
function editSideFlip(state) {
  state.view.setEditSide(state.view.getEditSide() === "w" ? "b" : "w");
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
    state.el.editCastlePopover.classList.remove("hidden");
    state.el.editCastleBtn.setAttribute("aria-expanded", "true");
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

// Ribbon button state, computed from the shared `state`.

// A finished AI-analysis turn in play mode: board frozen in ANALYZING, reads as
// paused, so the ribbon shows Resume (one click exits analysis + resumes play).
// Reads live state -- call, don't cache.
function aiAnalysisDone(state) {
  return state.analyzing && state.aiShared.turnFinished && state.aiEnabled;
}

function refreshButtons(state) {
  // Swap ribbons: edit overrides view, which overrides play.
  const activeRibbon = state.editing ? state.el.editRibbon : state.viewing ? state.el.viewRibbon : state.el.playRibbon;
  state.el.playRibbon.style.display = (state.viewing || state.editing) ? "none" : "";
  state.el.viewRibbon.style.display = (state.viewing && !state.editing) ? "" : "none";
  state.el.editRibbon.style.display = state.editing ? "" : "none";
  window.dispatchEvent(new CustomEvent(APP_EVT.RIBBON_ACTIVE, { detail: { el: activeRibbon } }));
  if (state.editing) {
    const isWhite = state.view.getEditSide() === "w";
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
      disabled: state.noEngine && !viewShowAsActive,
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
  const humanToMove = state.humanWhite ? state.turn === "white" : state.turn === "black";
  // Completed AI analysis in play mode reads as paused to the user; show
  // Resume (see aiAnalysisDone / onPause). Engine-only analysis and
  // in-progress runs keep the plain Pause/Resume toggle.
  const aiDone = aiAnalysisDone(state);
  const showResume = state.paused || aiDone;
  // Pause needs the human's turn; Resume is always allowed.
  configureBtn(state.el.pauseBtn, {
    disabled: state.gameOver || (!aiDone && state.analyzing) || (!showResume && !humanToMove),
    label: showResume ? "Resume" : "Pause",
    icon: showResume ? "forward-step" : "pause",
  });
  configureBtn(state.el.takebackBtn, {
    disabled: state.analyzing || state.gameOver || !state.allowTakeback || state.movesPlayed === 0,
  });
  configureBtn(state.el.savePgnBtn, { disabled: state.analyzing || state.movesPlayed === 0 });
  configureBtn(state.el.switchSidesBtn, { disabled: state.analyzing || state.gameOver || !state.resignAvailable });
  configureBtn(state.el.resignBtn, { disabled: state.paused || state.analyzing || state.gameOver || !state.resignAvailable });
  // AI turn finished but server is still ANALYZING (user hasn't
  // closed the AI window yet). Show the ribbon button as normal
  // ("Analysis mode", magnifying-glass, enabled) -- the rest of
  // the reachability gates (gameOver / no engine / not paused)
  // still apply.
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
    try {
      await stopAnalysisFromUi(state);
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
    const playerName = getConfiguredPlayerName();
    state.view.setGameId(null);
    state.view.setPlayerName(playerName);
    closeAi();
    const r = await state.ctx.api("POST", "/game/new", { player_name: playerName });
    state.view.setGameId(r.game_id);
    state.view.setHumanWhite(!!r.human_white);
    state.view.reset();
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

async function onPlayFromHereImpl(state) {
  if (state.playFromHereInflight) return;  // debounce double-click
  state.playFromHereInflight = true;
  setDisabled(state.el.viewPlayFromHereBtn, true);
  // Reset gameId so the racing board_update from new_game (which fires
  // BEFORE the API response carrying the new id) isn't dropped by the
  // game_id filter — that drop loses the human_white/name swap.
  state.view.setGameId(null);
  try {
    const playerName = getConfiguredPlayerName();
    state.view.setPlayerName(playerName);
    closeAi();
    const r = await state.ctx.api("POST", "/game/view/play-from-here", { player_name: playerName });
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
    reportError(state.ctx, MSG.PLAY_FROM_HERE_FAILED, e);
  } finally {
    state.playFromHereInflight = false;
    // Don't re-enable directly; state.refreshButtons() drives it next time
    // viewing flips, and by then the button is hidden anyway.
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
function showAnalysisToastImpl(state) {
  state.aiShared.dismissAnalysisToast?.();
  const msg = document.createElement("span");
  msg.className = "toast-sort-msg";
  const label = document.createElement("span");
  label.className = "toast-grow is-active";
  label.textContent = MSG.ANALYSIS_MODE;
  msg.append(label);
  msg.append(makeToastIconBtn("table-list", MSG.SEARCH_LINES, () => togglePvTableWindow(state.ctx.events)));
  msg.append(makeToastIconBtn("terminal", MSG.UCI_LOG, () => toggleUciLogWindow(state.ctx.events)));
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
async function startAnalysisFromUiImpl(state) {
  // Engine-only analysis: close any leftover AI panel from a prior AI run
  // before starting, so the dock shows engine-only output. Done first so
  // the close can't race the new analysis state.
  if (!state.aiEnabled && isAiOpen()) closeAi();
  await state.ctx.api("POST", "/game/analysis/start", {});
  restoreViewAnalysisWindows(state.ctx.events);
  state.aiShared.turnFinished = false;
  showAnalysisToastImpl(state);
  if (state.aiEnabled) {
    // Pin the title to the model actually about to run. Mid-session
    // provider/model edits do not retitle until the next Analyze click.
    setAiTitle(state.aiTitleModel);
    openAi();
    resetAi();
  }
}

async function onAnalyzeImpl(state) {
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
  try {
    await startAnalysisFromUiImpl(state);
  } catch (e) {
    reportError(state.ctx, MSG.START_ANALYSIS_FAILED, e);
  }
}

// Re-analyze: stop the current turn server-side (if any), then start a fresh
// one, keeping the AI panel open. reanalyzeInFlight guards against rapid
// double-clicks producing a spurious second start (server -> ModeConflictError).
async function onReanalyzeImpl(state) {
  if (state.reanalyzeInFlight) return;
  state.reanalyzeInFlight = true;
  try {
    if (state.analyzing) {
      snapshotViewAnalysisState();
      await state.ctx.api("POST", "/game/analysis/stop", {});
    }
    await startAnalysisFromUiImpl(state);
  } catch (e) {
    reportError(state.ctx, MSG.REANALYZE_FAILED, e);
  } finally {
    state.reanalyzeInFlight = false;
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
  const msg = document.createElement("span");
  msg.className = "toast-grow";
  msg.textContent = MSG.ENGINE_CRASHED;
  const node = document.createElement("span");
  node.className = "toast-sort-msg";
  let dismissCrashToast;
  node.append(msg, makeToastDismissBtn(() => dismissCrashToast?.()));
  dismissCrashToast = toast(node, { variant: "danger", duration: 0 });
}

// Commentary-dock visibility + server-authoritative edit-mode transitions.

function syncCommentsVisibility(state) {
  if (!state.el.commentsHost) return;
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
  _clearEditTransitionSuppression(state);
  state.pendingAnnotation = null;
  pushNavToUi(state);
  refreshButtons(state);
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
      dismissGameOverToast: null,
      pendingAnnotation: null,
      viewFlipped: false,
      takebackPending: false,
      commentNavPrev: null,
      commentNavNext: null,
      playFromHereInflight: false,
      reanalyzeInFlight: false,
      aiEnabled: false,
      aiTitleModel: "",
      noEngine: false,
      allowTakeback: true,
      movesPlayed: 0,
      viewTotalPlies: 0,
      viewGameOver: false,
      humanWhite: true,
      turn: "white",
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
    const editSideTogglePill = root.querySelector("#edit-side-toggle");
    const editCastleBtn = root.querySelector("#edit-castle-btn");
    const editCastlePopover = root.querySelector("#edit-castle-popover");
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
      editSideBtn, editSidePopover, editCastleBtn, editCastlePopover,
      editSideTogglePill, editCastleCb, editRibbon, playRibbon, viewRibbon,
      pauseBtn, takebackBtn, savePgnBtn, switchSidesBtn, resignBtn, analyzeBtn,
      viewFirstBtn, viewBackBtn, viewForwardBtn, viewLastBtn, viewSavePgnBtn,
      viewPlayFromHereBtn, viewAnalyzeBtn,
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
    noEngineBannerBtn.addEventListener("click", () => openSettings("engines"));
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
        try {
          await ctx.api("POST", "/game/move", { uci });
        } catch (e) {
          reportError(ctx, MSG.MOVE_REJECTED, e);
        }
      },
      // Click on a move in the list (view mode only) → jump cursor to
      // the position AFTER that move, i.e. ply = plyIndex + 1.
      onMoveJump: (plyIndex) => doViewNav(state, "/game/view/goto", { ply: plyIndex + 1 }),
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

    // Settings cache (refreshed on settings-changed).
    // True only while play->view->edit is in flight. Opening the dock
    // mid-transition fires a seeding /view/goto with the stale (pre-flip)
    // viewCursor=0, clobbering the live-position cursor the server lands
    // at via view_last(). Cleared in _onServerEditingStop.
    const commentsHost = root.querySelector(".play-comments-host");
    state.el.commentsHost = commentsHost;
    setCommentaryDockContainer(commentsHost);
    setAiInlineHost(root.querySelector(".play-ai-inline"));
    // X on the commentary window (dock slot or float) -> clear setting.
    setOnUserCloseCommentary(() => {
      state.showPgnComments = false;
      ctx.api("PUT", "/settings", { view_show_pgn_comments: false })
        .catch((e) => reportError(ctx, MSG.SETTING_SAVE_FAILED, e));
    });
    const onCommentsResize = () => { syncCommentsVisibility(state); };
    window.addEventListener("resize", onCommentsResize);

    // Closing the AI window mid-turn = same effect as clicking toolbar Stop:
    // snapshot view state, stop analysis, close all dock panels.
    setOnUserCloseAi(() => { stopAnalysisFromUi(state); });
    async function refreshSettings({ notifyOnDrift = false } = {}) {
      try {
        const s = await ctx.api("GET", "/settings");
        state.allowTakeback = s.allow_takeback !== false;
        state.showPgnComments = s.view_show_pgn_comments !== false;
        state.aiEnabled = !!s.ai_enabled;
        state.aiTitleModel = s.ai_enabled ? (s.ai_model || "") : "";
        syncCommentsVisibility(state);
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
    await refreshSettings();
    const onSettingsChanged = () => {
      refreshSettings({ notifyOnDrift: true }).then(() => refreshButtons(state));
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
    const onRecentsChanged = () => {
      if (state.viewing && state.viewingGameId) fetchXgameInfo(state, state.viewingGameId);
    };
    window.addEventListener(APP_EVT.RECENTS_CHANGED, onRecentsChanged);

    // Ask server to re-emit current state so the freshly-mounted view syncs.
    ctx.api("POST", "/game/sync", {}).catch(() => {});

    // View mode state (set from board_update.view payload).
    // Annotation staged by the user via the edit-mode annotation modal.
    // null  -> no pending change; /edit/commit goes with apply_comment=false.
    // ""    -> user explicitly cleared; server treats as delete.
    // "text"-> set/replace at the edit-entry ply.
    // Reset on every entry to edit mode and on /edit/cancel.
    const pausedBadge = document.getElementById("paused-badge");
    const finishedBadge = document.getElementById("finished-badge");
    function syncPausedUi() {
      const show = state.paused && !state.analyzing;
      boardHost.classList.toggle("board-paused", show);
      pausedBadge?.classList.toggle("hidden", !show);
    }
    function showFinishedBadge(text) {
      if (!finishedBadge) return;
      finishedBadge.textContent = text;
      finishedBadge.classList.toggle("hidden", !text);
    }

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
    const offEvent = ctx.events.on((evt) => {
      // AI events: buffer until replay completes, then dedupe by seq.
      if (evt.kind?.startsWith("ai_")) {
        if (ai.rehydrating) ai.liveBuffer.push(evt);
        else dispatchAiEventOrdered(ai, aiCtx, evt);
        return;
      }
      switch (evt.kind) {
        case "engine_search_start": {
          // Engine is busy. While the AI window is open this means the
          // agent is in a tool call; flip the status line so the user
          // sees what's taking time. The PV-table window consumes the
          // same event for its own reset; no conflict.
          if (isAiOpen()) setAiStatus("engine");
          break;
        }
        case "engine_info": {
          // Engine produced an info chunk -- search is delivering. Drop
          // the "engine searching" hint back to "waiting" so the user
          // knows the agent will narrate next.
          if (isAiOpen()) setAiStatus("waiting");
          break;
        }
        case "board_update": {
          _cachedBoardUpdate = evt;
          state.movesPlayed = evt.payload.moves_san?.length ?? 0;
          state.gameOver = false;
          showFinishedBadge("");
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
          state.viewing = !!v;
          // Read analyzing early: syncCommentsVisibility (called below) gates
          // view/goto on !analyzing; the main analyzing block runs later in
          // the same event but would be too late.
          if (typeof evt.payload.analyzing === "boolean") setAnalyzing(state, evt.payload.analyzing);
          if (state.viewing) {
            if (!wasViewing || state.viewingGameId !== prevGameId) {
              state.viewGameOverAlertShown = false;
              state.dismissGameOverToast?.();
              state.dismissGameOverToast = null;
              // Game switched (or first entry into view). Clear stale
              // x-game state synchronously and close any live toasts
              // BEFORE the in-band refreshXgameToasts (called later
              // in this handler) so it doesn't fire with stale data
              // from the prior game. fetchXgameInfo then populates
              // and re-renders.
              resetXgame(state);
              fetchXgameInfo(state, state.viewingGameId);
            }
            state.viewCursor = v.cursor ?? 0;
            state.viewTotalPlies = v.total_plies ?? 0;
            state.viewGameOver = !!v.game_over;
            state.lastViewComment = v.comment ?? null;
            _viewingHash = v.view_hash ?? null;
            _viewingSummary = v.view_summary ?? null;
            _viewing = true;
            // Single source of truth for commentary navigation. The server
            // ships fresh prev/next with every view payload, so game
            // switches (import while open) can't leave stale plies behind.
            state.commentNavPrev = v.prev_comment ?? null;
            state.commentNavNext = v.next_comment ?? null;
            pushNavToUi(state);
            syncCommentsVisibility(state);
            if (v.result) showFinishedBadge(resultBadge(v.result));
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
            view.setEnabled(false);
            // Restore the user's prior flip preference on entry into view mode.
            if (!wasViewing) view.setHumanWhite(!state.viewFlipped);
          } else {
            state.lastViewComment = null;
            _viewingHash = null;
            _viewingSummary = null;
            _viewing = false;
            state.commentNavPrev = null;
            state.commentNavNext = null;
            pushNavToUi(state);
            syncCommentsVisibility(state);
            if (wasViewing) restoreDebugWindows(ctx.events);
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
          if (typeof evt.payload.human_white === "boolean") {
            state.humanWhite = evt.payload.human_white;
          }
          if (evt.payload.turn) state.turn = evt.payload.turn;
          if (typeof evt.payload.analyzing === "boolean") {
            setAnalyzing(state, evt.payload.analyzing);
            // Don't re-enable interactivity in view mode regardless of
            // analysis state.
            if (!state.viewing) view.setEnabled(!state.analyzing && !state.paused);
            syncPausedUi();
            pushNavToUi(state);
            if (!state.analyzing) {
              state.aiShared.dismissAnalysisToast?.();
              state.aiShared.dismissAnalysisToast = null;
              if (state.viewing) { if (isAiOpen()) closeAi(); closeAnalysisOpenedWindows(); }
            } else if (!state.aiShared.dismissAnalysisToast) {
              // Server reports analysis active but no toast exists -- we
              // were re-mounted (e.g. user navigated to another
              // perspective and came back). Restore the toast so the
              // user can still see and dismiss it.
              showAnalysisToast();
            }
          }
          boardHost.classList.remove("board-idle");
          setDisabled(newGameBtn, false);
          refreshButtons(state);
          _playInProgress = state.movesPlayed > 0 && !state.gameOver && !state.viewing;
          break;
        }
        case "game_result":
          state.gameOver = true;
          state.paused = false;
          setAnalyzing(state, false);
          state.aiShared.dismissAnalysisToast?.();
          state.aiShared.dismissAnalysisToast = null;
          if (state.viewing) { if (isAiOpen()) closeAi(); closeAnalysisOpenedWindows(); }
          state.resignAvailable = false;
          setDisabled(newGameBtn, false);
          boardHost.classList.add("board-idle");
          syncPausedUi();
          showFinishedBadge(formatResult(evt.payload, state.humanWhite));
          refreshButtons(state);
          _playInProgress = false;
          showAlert({
            message: formatGameOver(evt.payload, state.humanWhite),
            messageClass: "game-over-message",
          });
          break;
        case "clock_tick":
          if (typeof evt.payload.paused === "boolean" && evt.payload.paused !== state.paused) {
            state.paused = evt.payload.paused;
            view.setEnabled(!state.paused);
            syncPausedUi();
            refreshButtons(state);
          }
          break;
      }
    });

    // Replay the last seen board_update (from a previous mount of this
    // perspective) so the view renders synchronously at the cached
    // position. Sent directly to the board renderer -- NOT through the
    // bus -- because play.js's bus handler has side effects (e.g.
    // syncCommentsVisibility issuing /view/goto) that would POST against
    // the current server game using stale cursor data when an external
    // import (tournament Replay) changed the active game while this
    // perspective was unmounted. The /sync POST above still fires and
    // the fresh board_update overrides if anything changed server-side.
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
    takebackBtn.addEventListener("click", onTakeback);
    switchSidesBtn.addEventListener("click", onSwitchSides);
    pauseBtn.addEventListener("click", onPause);
    pausedBadge?.addEventListener("click", onPause);
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
    editAnnotateBtn.addEventListener("click", onEditAnnotate);
    editConfirmBtn.addEventListener("click", onEditConfirm);
    editCancelBtn.addEventListener("click", onEditCancel);

    const offCrash = ctx.events.on(async (evt) => {
      if (evt.kind !== "system" || evt.payload?.error !== "engine_terminated") return;
      view.clearArrows();
      if (state.analyzing) {
        await onAnalyze();
      }
      showEngineCrashToast();
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
        if (state.editing) {
          // Fire-and-forget cancel so the server doesn't stay stuck in
          // edit mode if the user navigates away.
          ctx.api("POST", "/game/edit/cancel", {}).catch(() => {});
          view.exitEditMode();
        }
        closeDebugWindows();
        // Announce no active ribbon so the global float manager unmounts it.
        window.dispatchEvent(new CustomEvent(APP_EVT.RIBBON_ACTIVE, { detail: { el: null } }));
        setDockContainer(null);
        closeCommentary();
        setCommentaryDockContainer(null);
        setOnUserCloseCommentary(null);
        closeAi();
        setAiInlineHost(null);
        setOnUserCloseAi(null);
        setOnReanalyzeAi(null);
        state.aiShared.dismissAnalysisToast?.();
        state.aiShared.dismissAnalysisToast = null;
        state.dismissGameOverToast?.();
        state.dismissGameOverToast = null;
        pausedBadge?.classList.add("hidden");
        showFinishedBadge("");
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
        editConfirmBtn.removeEventListener("click", onEditConfirm);
        editCancelBtn.removeEventListener("click", onEditCancel);
      },
    };
  },
};
