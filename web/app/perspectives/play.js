// Play perspective: human vs engine.
// Composes the reusable GameView with a game-control bar (New / Take back /
// Resign).

import { mountGameView } from "../game-view.js";
import { alert as showAlert, confirm, makeToastDismissBtn, openSettings, reportError, toast } from "../dialogs.js";
import { showImportPositionDialog, confirmReplaceViewedGame, confirmDiscardViewedGame } from "../import-position-dialog.js";
import { toggleUciLogWindow, togglePvTableWindow, closeDebugWindows, closeDebugWindowsPersist, restoreDebugWindows, snapshotViewAnalysisState, restoreViewAnalysisWindows, setDockContainer, isMobileLayout } from "../play-dock-windows.js";
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
  appendAiToolCall,
  markAiToolCallFailed,
  noteAiRevision,
  markAiDone,
  setAiStatus,
  setAiTitle,
  setOnUserCloseAi,
  setOnReanalyzeAi,
  isAiOpen,
} from "../play-ai-window.js";
import { terminationLabel } from "../format-termination.js";
import { editAnnotation } from "../annotation-dialog.js";
import { getConfiguredPlayerName } from "../settings-dialog.js";

// Tool name the AI uses to inspect hypothetical positions; the live
// board mirrors `input.fen` while a call with this name is in flight.
const ANALYZE_TOOL_NAME = "analyze";

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
  return result === "1/2-1/2" ? "½-½" : result;
}

function formatResult(payload, humanWhite) {
  const { result, by, loser } = payload;
  if (result === "1-0" || result === "0-1") return result;
  if (result === "1/2-1/2") return resultBadge(result);
  if (result === "resign") {
    const humanLost = by === "human";
    const whiteWins = humanLost ? !humanWhite : humanWhite;
    return whiteWins ? "1-0" : "0-1";
  }
  if (result === "timeout") {
    return loser === "white" ? "0-1" : "1-0";
  }
  return "";
}

function formatViewGameOver({ result, termination }) {
  const reason = terminationLabel(termination);
  if (result === "1-0") return `${reason} -- White wins.`;
  if (result === "0-1") return `${reason} -- Black wins.`;
  if (result === "1/2-1/2") return `${reason} -- Draw.`;
  return reason;
}

function formatGameOver(payload, humanWhite) {
  const { result, termination, by, loser } = payload;
  if (result === "resign") {
    return by === "human" ? "You resigned." : "Engine resigned.";
  }
  if (result === "timeout") {
    const humanLost = (loser === "white") === humanWhite;
    return humanLost ? "You lost on time." : "Engine lost on time.";
  }
  const reason = terminationLabel(termination);
  if (result === "1-0" || result === "0-1") {
    const humanWon = (result === "1-0") === humanWhite;
    return `${reason} -- ${humanWon ? "you win" : "engine wins"}.`;
  }
  return `${reason} -- Draw.`;
}

const ANALYZE_LABEL_STOP = "Stop analysis";
const ANALYZE_LABEL_START = "Analysis mode";
const ANALYZE_ICON_STOP = "magnifying-glass-minus";
const ANALYZE_ICON_START = "magnifying-glass-plus";
// Body class set while analysis is on; CSS greys + inert-ifies x-game
// nav links so the user can't jump games mid-analysis.
const XGAME_LOCK_CLASS = "xgame-nav-locked";

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

export const playPerspective = {
  id: "play",
  label: "Play",

  async mount(root, ctx) {
    root.innerHTML = `
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

    // Tracks "server has zero engines registered." Drives both the
    // CTA banner and per-button gating (view-analyze, AI settings).
    // Mirrored in JS state so refreshButtons() can read it without an
    // extra DOM query each call.
    let noEngine = false;
    let buttonsReady = false; // refreshButtons reads `editing` etc.; safe only after their let-bindings
    function setNoEngine(v) {
      noEngine = !!v;
      noEngineBanner.classList.toggle("hidden", !noEngine);
      if (buttonsReady) refreshButtons();
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
    window.addEventListener("sturddle:engines-changed", onEnginesChanged);
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
          reportError(ctx, "Move rejected", e);
        }
      },
      // Click on a move in the list (view mode only) → jump cursor to
      // the position AFTER that move, i.e. ply = plyIndex + 1.
      onMoveJump: (plyIndex) => doViewNav("/game/view/goto", { ply: plyIndex + 1 }),
      // Fork glyphs. Fresh map per render; both child-here (this game
      // has forks at this ply) and own-fork-ply (this game itself
      // diverged from its parent here) get a glyph.
      forkInfoFn: () => {
        const m = new Map();
        for (const c of xgame.children || []) {
          // fork_ply is 1-based; cell index = fork_ply - 1.
          const idx = (c.fork_ply ?? 0) - 1;
          if (idx < 0) continue;
          const cur = m.get(idx) ?? { childCount: 0, isOwnForkPly: false };
          cur.childCount += 1;
          m.set(idx, cur);
        }
        if (xgame.parentGameId && xgame.forkPly != null) {
          const idx = xgame.forkPly - 1;
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
        if (analyzing) return;
        xgame.parentToastDismissed = false;
        xgame.childrenToastDismissed = false;
        _setXgameDismissed(xgame.gameId, "parent", false);
        _setXgameDismissed(xgame.gameId, "children", false);
        if (viewCursor === plyIndex + 1) {
          refreshXgameToasts();
          return;
        }
        doViewNav("/game/view/goto", { ply: plyIndex + 1 });
      },
    });

    // Settings cache (refreshed on settings-changed).
    let allowTakeback = true;
    let showPgnComments = true; // view-mode commentary window
    // True only while play->view->edit is in flight. Opening the dock
    // mid-transition fires a seeding /view/goto with the stale (pre-flip)
    // viewCursor=0, clobbering the live-position cursor the server lands
    // at via view_last(). Cleared in _onServerEditingStop.
    let suppressCommentsForEditTransition = false;
    const commentsHost = root.querySelector(".play-comments-host");
    setCommentaryDockContainer(commentsHost);
    let lastViewComment = null;
    // X on the commentary window (dock slot or float) -> clear setting.
    setOnUserCloseCommentary(() => {
      showPgnComments = false;
      ctx.api("PUT", "/settings", { view_show_pgn_comments: false })
        .catch((e) => reportError(ctx, "Failed to save setting", e));
    });
    function syncCommentsVisibility() {
      if (!commentsHost) return;
      const shouldShow = viewing && showPgnComments && !isMobileLayout()
        && !suppressCommentsForEditTransition;
      const open = isCommentaryOpen();
      if (shouldShow) {
        if (!open) openCommentary();
        setCommentaryText(lastViewComment);
      } else if (open) {
        commentNavPrev = null;
        commentNavNext = null;
        closeCommentary();
      }
    }
    const onCommentsResize = () => { syncCommentsVisibility(); };
    window.addEventListener("resize", onCommentsResize);

    // --- AI analysis lifecycle ---
    // Master toggle from settings; gates the AI panel + the server-side
    // start_analysis branch. The AI window lives in the main dock
    // alongside Search Lines + UCI Log.
    let aiEnabled = false;
    // Closing the AI window mid-turn = same effect as clicking
    // toolbar Stop: snapshot view state, stop analysis, close all
    // dock panels. stopAnalysisFromUi is defined further down.
    setOnUserCloseAi(() => { stopAnalysisFromUi(); });
    // Snapshot of TC fields used at the start of the current game; lets
    // us tell the user "applies on next game" if they edit TC mid-play.
    let gameTcInitial = null;
    let gameTcIncrement = null;
    // Latest AI provider/model from settings, captured at analyze-start
    // time. We do not write to setAiTitle on every settings refresh --
    // the panel title should reflect what is actually running, not what
    // is selected in Settings.
    let aiTitleModel = "";
    async function refreshSettings({ notifyOnDrift = false } = {}) {
      try {
        const s = await ctx.api("GET", "/settings");
        allowTakeback = s.allow_takeback !== false;
        showPgnComments = s.view_show_pgn_comments !== false;
        aiEnabled = !!s.ai_enabled;
        aiTitleModel = s.ai_enabled ? (s.ai_model || "") : "";
        syncCommentsVisibility();
        if (notifyOnDrift && !gameOver && resignAvailable) {
          const drift = [];
          // TC: compare against the snapshot taken at game start.
          if (
            gameTcInitial !== null &&
            (Number(s.tc_initial_seconds) !== gameTcInitial ||
              Number(s.tc_increment_seconds) !== gameTcIncrement)
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
        if (!resignAvailable) {
          gameTcInitial = Number(s.tc_initial_seconds);
          gameTcIncrement = Number(s.tc_increment_seconds);
        }
      } catch {
        // ignore
      }
    }
    await refreshSettings();
    const onSettingsChanged = () => {
      refreshSettings({ notifyOnDrift: true }).then(() => refreshButtons());
    };
    window.addEventListener("sturddle:settings-changed", onSettingsChanged);

    // sturddle:layout-changed fires when ribbon_float toggled in main.js or
    // when the user closes the ribbon WinBox. Re-run refreshButtons so the
    // active ribbon is mounted in the WinBox (or unhidden from the DOM).
    const onLayoutChanged = () => { refreshButtons(); };
    window.addEventListener("sturddle:layout-changed", onLayoutChanged);

    // sturddle:recents-changed fires when another perspective (e.g. the
    // import dialog) mutated the recents store. Re-fetch x-game info
    // for the currently-viewed game so the fork glyph + banner reflect
    // the new state (B3: glyph stale after a child was deleted).
    const onRecentsChanged = () => {
      if (viewing && viewingGameId) fetchXgameInfo(viewingGameId);
    };
    window.addEventListener("sturddle:recents-changed", onRecentsChanged);

    // Ask server to re-emit current state so the freshly-mounted view syncs.
    ctx.api("POST", "/game/sync", {}).catch(() => {});

    // Track whether the current game is in progress (any moves played and
    // not yet ended). Drives New Game enable + confirm semantics.
    // TODO: humanWhite + turn are also held inside GameView; consider
    // exposing getters on `view` and dropping these locals (single source).
    let movesPlayed = 0;
    let gameOver = false;
    let humanWhite = true;
    let turn = "white";
    let paused = false;
    let analyzing = false;
    // AI mode only: flips true when an AI turn terminates naturally
    // (not cancelled/error). Server stays in ANALYSIS so the board is
    // locked, but the ribbon stops shouting "stopping..." and the
    // toast disappears. Reset on next analyze start.
    let aiTurnFinished = false;
    // Single sync point: every analyzing write goes through this setter so
    // the module-scope mirror (_analyzing) used by isAnalyzing() stays
    // current. Direct `analyzing = ...` writes will drift -- always call
    // setAnalyzing instead.
    function setAnalyzing(v) {
      analyzing = !!v;
      _analyzing = analyzing;
      // Server flipped out of ANALYSIS -- clear the AI-finished latch
      // so the ribbon can re-enable when the game is paused again.
      if (!analyzing) aiTurnFinished = false;
      document.body.classList.toggle(XGAME_LOCK_CLASS, analyzing);
    }
    // View mode state (set from board_update.view payload).
    let viewing = false;
    let viewCursor = 0;
    let viewTotalPlies = 0;
    let viewGameOver = false;
    let viewGameOverAlertShown = false;
    let dismissGameOverToast = null;
    let viewingGameId = null;
    // X-game navigation state. Populated by fetchXgameInfo after every
    // view-game change; cleared when game_id flips. Toast dismiss flags
    // are per-game don't-nag (only explicit X resets them; ply-change
    // auto-close does NOT count). Toast handles are dismiss callbacks
    // from toast() -- kept so we can close on ply change.
    let xgame = {
      gameId: null,
      parentGameId: null,
      parentSummary: null,
      forkPly: null,
      children: [],
      parentToastDismissed: false,
      childrenToastDismissed: false,
      parentToastHandle: null,
      childrenToastHandle: null,
    };
    function closeXgameToasts() {
      if (xgame.parentToastHandle) { xgame.parentToastHandle(); xgame.parentToastHandle = null; }
      if (xgame.childrenToastHandle) { xgame.childrenToastHandle(); xgame.childrenToastHandle = null; }
    }
    function resetXgame() {
      closeXgameToasts();
      xgame.gameId = null;
      xgame.parentGameId = null;
      xgame.parentSummary = null;
      xgame.forkPly = null;
      xgame.children = [];
      xgame.parentToastDismissed = false;
      xgame.childrenToastDismissed = false;
    }
    async function fetchXgameInfo(gameId) {
      if (!gameId) {
        resetXgame();
        refreshXgameToasts();
        return;
      }
      try {
        const r = await ctx.api(
          "GET", `/game/recent-imports/by-id/${encodeURIComponent(gameId)}`,
        );
        xgame.gameId = gameId;
        xgame.parentGameId = r.parent_game_id ?? null;
        xgame.parentSummary = r.parent_summary ?? null;
        xgame.forkPly = r.fork_ply ?? null;
        xgame.children = Array.isArray(r.children) ? r.children : [];
        // Hydrate per-game don't-nag flags from the module-level map so
        // an explicit X survives perspective remount within the same
        // page load.
        const d = _getXgameDismissed(gameId);
        xgame.parentToastDismissed = d.parent;
        xgame.childrenToastDismissed = d.children;
        // After data lands, re-render the move list so glyphs appear
        // without waiting for the next board_update.
        if (_cachedBoardUpdate) view.applyEvent(_cachedBoardUpdate);
        refreshXgameToasts();
      } catch (_e) {
        // The current game may not be in recents (e.g. brand-new play
        // game with no moves yet). That's expected; just clear state.
        resetXgame();
        refreshXgameToasts();
      }
    }
    function formatGameLabel(summary) {
      const s = summary || {};
      const white = s.white || "?";
      const black = s.black || "?";
      const result = s.result && s.result !== "*" ? ` (${s.result})` : "";
      return `${white} vs ${black}${result}`;
    }
    function buildParentToast() {
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
      text.append("Forked from ");
      const link = document.createElement("button");
      link.className = "xgame-link";
      link.type = "button";
      link.textContent = xgame.parentSummary
        ? formatGameLabel(xgame.parentSummary)
        : "parent game";
      link.addEventListener("click", () => {
        if (analyzing) return;
        if (xgame.parentGameId) {
          openXgameTarget(xgame.parentGameId, { landAtPly: xgame.forkPly });
        }
      });
      text.append(link);
      text.append(` at ply ${xgame.forkPly ?? "?"}.`);
      node.append(text);
      node.append(makeToastDismissBtn(() => {
        xgame.parentToastDismissed = true;
        _setXgameDismissed(xgame.gameId, "parent", true);
        if (xgame.parentToastHandle) {
          xgame.parentToastHandle();
          xgame.parentToastHandle = null;
        }
      }));
      return node;
    }
    function buildChildrenToast(childrenHere) {
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
          if (analyzing) return;
          openXgameTarget(c.game_id, { landAtPly: c.fork_ply });
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
      arrow.setAttribute("aria-label", "Show variations");
      arrow.title = "Show variations";
      const arrowIcon = document.createElement("wa-icon");
      arrowIcon.setAttribute("name", "chevron-up");
      arrow.append(arrowIcon);
      arrow.addEventListener("click", () => {
        const expanded = !list.classList.toggle("hidden");
        arrowIcon.setAttribute("name", expanded ? "chevron-down" : "chevron-up");
      });
      header.append(arrow);
      header.append(makeToastDismissBtn(() => {
        xgame.childrenToastDismissed = true;
        _setXgameDismissed(xgame.gameId, "children", true);
        if (xgame.childrenToastHandle) {
          xgame.childrenToastHandle();
          xgame.childrenToastHandle = null;
        }
      }));
      node.append(header);
      return node;
    }
    function refreshXgameToasts() {
      // Child -> parent: cursor lands precisely on the fork ply of the
      // current child + the game has a parent. Auto-close on ply
      // change (does NOT count as a dismiss).
      const showParent = viewing
        && xgame.parentGameId
        && xgame.forkPly != null
        && viewCursor === xgame.forkPly
        && lastViewNavKind === "precise"
        && !xgame.parentToastDismissed;
      if (showParent && !xgame.parentToastHandle) {
        xgame.parentToastHandle = toast(buildParentToast(), {
          variant: "neutral", duration: 0, stack: "xgame",
        });
      } else if (!showParent && xgame.parentToastHandle) {
        xgame.parentToastHandle();
        xgame.parentToastHandle = null;
      }
      // Parent -> child: cursor lands precisely on a fork ply that has
      // 1+ children. Same auto-close-on-ply-change rule.
      const childrenHere = (xgame.children || []).filter(
        c => (c.fork_ply ?? -1) === viewCursor,
      );
      const showChildren = viewing
        && lastViewNavKind === "precise"
        && childrenHere.length > 0
        && !xgame.childrenToastDismissed;
      if (showChildren && !xgame.childrenToastHandle) {
        xgame.childrenToastHandle = toast(buildChildrenToast(childrenHere), {
          variant: "neutral", duration: 0, stack: "xgame",
        });
      } else if (!showChildren && xgame.childrenToastHandle) {
        xgame.childrenToastHandle();
        xgame.childrenToastHandle = null;
      }
    }
    async function openXgameTarget(gameId, opts = {}) {
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
        viewing
        && viewingGameId === gameId
        && (landAtPly === null || viewCursor === landAtPly)
      ) {
        return;
      }
      try {
        const target = await ctx.api(
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
          lastViewNavKind = "precise";
        }
        // GameView's applyEvent drops board_updates whose game_id does
        // NOT match its local gameId. Clear before import so the
        // server's fresh game_id is accepted; set it to the returned
        // id so subsequent updates are still scoped.
        view.setGameId(null);
        closeAi();
        const r = await ctx.api("POST", "/game/import", importPayload);
        if (r?.game_id) view.setGameId(r.game_id);
      } catch (e) {
        reportError(ctx, "Open game failed", e);
      }
    }
    let editing = false;
    // Annotation staged by the user via the edit-mode annotation modal.
    // null  -> no pending change; /edit/commit goes with apply_comment=false.
    // ""    -> user explicitly cleared; server treats as delete.
    // "text"-> set/replace at the edit-entry ply.
    // Reset on every entry to edit mode and on /edit/cancel.
    let pendingAnnotation = null;
    const pausedBadge = document.getElementById("paused-badge");
    const finishedBadge = document.getElementById("finished-badge");
    function syncPausedUi() {
      const show = paused && !analyzing;
      boardHost.classList.toggle("board-paused", show);
      pausedBadge?.classList.toggle("hidden", !show);
    }
    function showFinishedBadge(text) {
      if (!finishedBadge) return;
      finishedBadge.textContent = text;
      finishedBadge.classList.toggle("hidden", !text);
    }
    let dismissAnalysisToast = null;

    // Resign is enabled whenever there is an active game; cleared on
    // game_result. We track it explicitly so paused-state can additionally
    // gate it without losing the "active game" signal.
    let resignAvailable = false;
    buttonsReady = true;
    function refreshButtons() {
      // Swap ribbons: edit overrides view, which overrides play.
      const activeRibbon = editing ? editRibbon : viewing ? viewRibbon : playRibbon;
      playRibbon.style.display = (viewing || editing) ? "none" : "";
      viewRibbon.style.display = (viewing && !editing) ? "" : "none";
      editRibbon.style.display = editing ? "" : "none";
      window.dispatchEvent(new CustomEvent("sturddle:ribbon-active", { detail: { el: activeRibbon } }));
      if (editing) {
        const isWhite = view.getEditSide() === "w";
        editSideBtn.setAttribute("aria-label", `Side to move: ${isWhite ? "White" : "Black"}`);
        editSideBtn.setAttribute("title", `Side to move: ${isWhite ? "White" : "Black"}`);
        editSideBtn.classList.toggle("is-active", !isWhite);
        editSideTogglePill.textContent = isWhite ? "White to move" : "Black to move";
        editSideTogglePill.classList.toggle("is-black", !isWhite);
        editSideTogglePill.setAttribute("aria-pressed", isWhite ? "false" : "true");
        const rights = view.getCastlingRights();
        for (const [k, btn] of Object.entries(editCastleCb)) {
          btn.setAttribute("aria-pressed", rights[k] ? "true" : "false");
          btn.classList.toggle("is-active", rights[k]);
        }
        const anyRight = rights.wK || rights.wQ || rights.bK || rights.bQ;
        editCastleBtn.classList.toggle("is-active", anyRight);
        return;
      }
      if (viewing) {
        const atStart = viewCursor === 0;
        const atEnd = viewCursor === viewTotalPlies;
        configureBtn(viewFirstBtn, { disabled: analyzing || atStart });
        configureBtn(viewBackBtn, { disabled: analyzing || atStart });
        configureBtn(viewForwardBtn, { disabled: analyzing || atEnd });
        configureBtn(viewLastBtn, { disabled: analyzing || atEnd });
        configureBtn(viewSavePgnBtn, { disabled: analyzing || viewTotalPlies === 0 });
        // Play-from-here is rejected at game-over plies (checkmate /
        // stalemate / draw). Backed by a backend guard that prevents
        // half-cleared state if the UI is bypassed.
        configureBtn(viewPlayFromHereBtn, { disabled: analyzing || viewGameOver });
        // Engine-less view: analyze is unreachable. Tooltip points at
        // Engines tab so the user knows the next step.
        // AI turn finished but server still ANALYZING: show ribbon as
        // normal ("Analysis mode") even though `analyzing` is true.
        const viewShowAsActive = analyzing && !aiTurnFinished;
        configureBtn(viewAnalyzeBtn, {
          disabled: noEngine && !viewShowAsActive,
          active: viewShowAsActive,
          label: viewShowAsActive
            ? ANALYZE_LABEL_STOP
            : noEngine
              ? "Register an engine in Settings to analyze"
              : ANALYZE_LABEL_START,
          icon: viewShowAsActive ? ANALYZE_ICON_STOP : ANALYZE_ICON_START,
        });
        return;
      }
      const humanToMove = humanWhite ? turn === "white" : turn === "black";
      // Pause is restricted to the human's turn; Resume (paused=true) is
      // always allowed so a game paused on the engine's turn — e.g. after
      // exiting Analysis — can be unpaused.
      configureBtn(pauseBtn, {
        disabled: gameOver || analyzing || (!paused && !humanToMove),
        label: paused ? "Resume" : "Pause",
        icon: paused ? "forward-step" : "pause",
      });
      configureBtn(takebackBtn, {
        disabled: analyzing || gameOver || !allowTakeback || movesPlayed === 0,
      });
      configureBtn(savePgnBtn, { disabled: analyzing || movesPlayed === 0 });
      configureBtn(switchSidesBtn, { disabled: analyzing || gameOver || !resignAvailable });
      configureBtn(resignBtn, { disabled: paused || analyzing || gameOver || !resignAvailable });
      // AI turn finished but server is still ANALYZING (user hasn't
      // closed the AI window yet). Show the ribbon button as normal
      // ("Analysis mode", magnifying-glass, enabled) -- the rest of
      // the reachability gates (gameOver / no engine / not paused)
      // still apply.
      const showAsActive = analyzing && !aiTurnFinished;
      const analyzeReachable = !gameOver && resignAvailable && (paused || aiTurnFinished);
      configureBtn(analyzeBtn, {
        disabled: !showAsActive && !analyzeReachable,
        active: showAsActive,
        label: showAsActive ? ANALYZE_LABEL_STOP : ANALYZE_LABEL_START,
        icon: showAsActive ? ANALYZE_ICON_STOP : ANALYZE_ICON_START,
      });
    }

    // View-mode flip is purely visual (no backend state; the user isn't
    // playing yet so "which side am I" is meaningless). Persisted so it
    // survives perspective remounts; applied on each entry into view mode.
    const VIEW_FLIP_KEY = "sturddle:view:flipped";
    let viewFlipped = false;
    try { viewFlipped = localStorage.getItem(VIEW_FLIP_KEY) === "1"; } catch { /* */ }

    // AI event handling extracted so the same dispatch can replay
    // buffered events on perspective remount (panel rehydration when a
    // mid-turn reconnect happens).
    function dispatchAiEvent(evt) {
      switch (evt.kind) {
        case "ai_info": {
          const p = evt.payload || {};
          if (typeof p.delta === "string") appendAiDelta(p.delta, p.round ?? 0);
          if (p.done) {
            // Defensive: tool-call lifecycle can drop the restore signal
            // (cancelled mid-call, round cap, etc.). Always snap back.
            view.restorePosition({ animate: false });
            markAiDone({
              cancelled: !!p.cancelled,
              error: p.error || null,
              errorDetail: p.error_detail || null,
              roundCap: !!p.round_cap,
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
              aiTurnFinished = true;
              dismissAnalysisToast?.();
              dismissAnalysisToast = null;
              refreshButtons();
            }
          }
          return true;
        }
        case "ai_thinking": {
          const p = evt.payload || {};
          if (typeof p.delta === "string") appendAiThinking(p.delta, p.round ?? 0);
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
        case "ai_corrective": {
          const p = evt.payload || {};
          noteAiRevision({
            round: p.round ?? 0,
            illegalMoves: p.illegal_moves || [],
            falseClaims: p.false_claims || [],
            castleViolations: p.castle_violations || [],
          });
          return true;
        }
      }
      return false;
    }

    // Buffer live AI events while the replay GET is in flight, then
    // drain in seq order with dedupe. Avoids the GET-then-subscribe
    // race: live events that fire between subscribe and replay arrival
    // are held instead of dispatched out-of-order.
    let aiRehydrating = true;
    let aiLiveBuffer = [];
    let aiMaxSeq = 0;
    function dispatchAiEventOrdered(evt) {
      const seq = evt?.payload?.seq ?? 0;
      // Server resets seq to 1 at the start of each turn, so seq=1
      // unconditionally marks a new turn and resets the high-water mark.
      // Otherwise a single-event turn (e.g. instant error) following a
      // prior turn whose aiMaxSeq is also 1 would be swallowed.
      if (seq === 1) aiMaxSeq = 0;
      else if (seq && seq <= aiMaxSeq) return;
      if (seq) aiMaxSeq = seq;
      dispatchAiEvent(evt);
    }
    async function rehydrateAiPanel() {
      try {
        const r = await ctx.api("GET", "/game/analysis/replay");
        const events = Array.isArray(r?.events) ? r.events : [];
        if (events.length > 0) {
          openAi();
          resetAi();
          for (const evt of events) dispatchAiEventOrdered(evt);
        }
      } catch { /* */ } finally {
        aiRehydrating = false;
        const buffered = aiLiveBuffer;
        aiLiveBuffer = [];
        for (const evt of buffered) dispatchAiEventOrdered(evt);
      }
    }
    rehydrateAiPanel();

    // --- Hook events for control-bar state changes (board state changes
    //     are GameView's responsibility). ---
    const offEvent = ctx.events.on((evt) => {
      // AI events: buffer until replay completes, then dedupe by seq.
      if (evt.kind?.startsWith("ai_")) {
        if (aiRehydrating) aiLiveBuffer.push(evt);
        else dispatchAiEventOrdered(evt);
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
          movesPlayed = evt.payload.moves_san?.length ?? 0;
          gameOver = false;
          showFinishedBadge("");
          // Server-authoritative edit state. Transitions drive the client
          // editor extension on/off; the ribbon UI follows `editing`.
          const wasEditing = editing;
          editing = !!evt.payload.editing;
          if (editing && !wasEditing) _onServerEditingStart();
          else if (!editing && wasEditing) _onServerEditingStop();
          // View mode swaps the ribbon and suppresses play-mode signals
          // (resignAvailable, etc.) — the user isn't playing yet.
          const v = evt.payload.view;
          const wasViewing = viewing;
          const prevGameId = viewingGameId;
          viewingGameId = evt.game_id ?? null;
          viewing = !!v;
          // Read analyzing early: syncCommentsVisibility (called below) gates
          // view/goto on !analyzing; the main analyzing block runs later in
          // the same event but would be too late.
          if (typeof evt.payload.analyzing === "boolean") setAnalyzing(evt.payload.analyzing);
          if (viewing) {
            if (!wasViewing || viewingGameId !== prevGameId) {
              viewGameOverAlertShown = false;
              dismissGameOverToast?.();
              dismissGameOverToast = null;
              // Game switched (or first entry into view). Clear stale
              // x-game state synchronously and close any live toasts
              // BEFORE the in-band refreshXgameToasts (called later
              // in this handler) so it doesn't fire with stale data
              // from the prior game. fetchXgameInfo then populates
              // and re-renders.
              resetXgame();
              fetchXgameInfo(viewingGameId);
            }
            viewCursor = v.cursor ?? 0;
            viewTotalPlies = v.total_plies ?? 0;
            viewGameOver = !!v.game_over;
            lastViewComment = v.comment ?? null;
            _viewingHash = v.view_hash ?? null;
            _viewingSummary = v.view_summary ?? null;
            _viewing = true;
            // Single source of truth for commentary navigation. The server
            // ships fresh prev/next with every view payload, so game
            // switches (import while open) can't leave stale plies behind.
            commentNavPrev = v.prev_comment ?? null;
            commentNavNext = v.next_comment ?? null;
            pushNavToUi();
            syncCommentsVisibility();
            if (v.result) showFinishedBadge(resultBadge(v.result));
            if (viewGameOver && viewCursor === viewTotalPlies && v.result && !viewGameOverAlertShown) {
              viewGameOverAlertShown = true;
              const node = document.createElement("span");
              node.className = "toast-sort-msg";
              const msg = document.createElement("span");
              msg.className = "toast-grow";
              msg.textContent = formatViewGameOver(v);
              node.append(msg, makeToastDismissBtn(() => { dismissGameOverToast?.(); dismissGameOverToast = null; }));
              dismissGameOverToast = toast(node, { variant: "neutral", duration: 6000 });
            }
            resignAvailable = false;
            // Board is read-only in view mode; the user navigates via ribbon.
            view.setEnabled(false);
            // Restore the user's prior flip preference on entry into view mode.
            if (!wasViewing) view.setHumanWhite(!viewFlipped);
          } else {
            lastViewComment = null;
            _viewingHash = null;
            _viewingSummary = null;
            _viewing = false;
            commentNavPrev = null;
            commentNavNext = null;
            pushNavToUi();
            syncCommentsVisibility();
            if (wasViewing) restoreDebugWindows(ctx.events);
            // Leaving view mode -- x-game state is per-viewed-game; drop it.
            if (wasViewing) resetXgame();
            resignAvailable = true;
          }
          // Cursor or viewing state may have just changed -- re-evaluate
          // the parent / children toasts. Open/close as needed.
          refreshXgameToasts();
          // Notify the perspective router so the nav label can swap
          // Play <-> View when the mode flips.
          if (wasViewing !== viewing) {
            window.dispatchEvent(new CustomEvent("sturddle:viewing-changed", {
              detail: { viewing },
            }));
          }
          if (typeof evt.payload.human_white === "boolean") {
            humanWhite = evt.payload.human_white;
          }
          if (evt.payload.turn) turn = evt.payload.turn;
          if (typeof evt.payload.analyzing === "boolean") {
            setAnalyzing(evt.payload.analyzing);
            // Don't re-enable interactivity in view mode regardless of
            // analysis state.
            if (!viewing) view.setEnabled(!analyzing && !paused);
            syncPausedUi();
            pushNavToUi();
            if (!analyzing) {
              dismissAnalysisToast?.();
              dismissAnalysisToast = null;
              if (viewing) closeDebugWindowsPersist();
            } else if (!dismissAnalysisToast) {
              // Server reports analysis active but no toast exists -- we
              // were re-mounted (e.g. user navigated to another
              // perspective and came back). Restore the toast so the
              // user can still see and dismiss it.
              showAnalysisToast();
            }
          }
          boardHost.classList.remove("board-idle");
          setDisabled(newGameBtn, false);
          refreshButtons();
          _playInProgress = movesPlayed > 0 && !gameOver && !viewing;
          break;
        }
        case "game_result":
          gameOver = true;
          paused = false;
          setAnalyzing(false);
          dismissAnalysisToast?.();
          dismissAnalysisToast = null;
          if (viewing) closeDebugWindowsPersist();
          resignAvailable = false;
          setDisabled(newGameBtn, false);
          boardHost.classList.add("board-idle");
          syncPausedUi();
          showFinishedBadge(formatResult(evt.payload, humanWhite));
          refreshButtons();
          _playInProgress = false;
          showAlert({
            message: formatGameOver(evt.payload, humanWhite),
            messageClass: "game-over-message",
          });
          break;
        case "clock_tick":
          if (typeof evt.payload.paused === "boolean" && evt.payload.paused !== paused) {
            paused = evt.payload.paused;
            view.setEnabled(!paused);
            syncPausedUi();
            refreshButtons();
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

    // Prompt before discarding an active play game. Returns true if the
    // caller should proceed (no active game, or user confirmed).
    async function _confirmDiscardActiveGame({ message, okLabel }) {
      if (!_playInProgress) return true;
      return await confirm({
        message,
        okLabel,
        cancelLabel: "Keep playing",
        destructive: true,
      });
    }

    // Prompt before replacing the currently viewed game. Skips when nothing
    // is being viewed or when the incoming hash matches the current view.
    async function _confirmReplaceViewedGame({ incomingHash, incomingSummary }) {
      if (!viewing) return true;
      return await confirmReplaceViewedGame({
        currentHash: _viewingHash,
        currentSummary: _viewingSummary,
        incomingHash,
        incomingSummary,
        analysisRunning: analyzing,
      });
    }

    const onNewGame = async () => {
      if (_playInProgress) {
        if (!await _confirmDiscardActiveGame({
          message: "Cancel the game in progress and start a new one?",
          okLabel: "New game",
        })) return;
      } else {
        const ok = await confirmDiscardViewedGame({
          viewing,
          currentSummary: _viewingSummary,
          analysisRunning: analyzing,
        });
        if (!ok) return;
      }
      try {
        const playerName = getConfiguredPlayerName();
        view.setGameId(null);
        view.setPlayerName(playerName);
        closeAi();
        const r = await ctx.api("POST", "/game/new", { player_name: playerName });
        view.setGameId(r.game_id);
        view.setHumanWhite(!!r.human_white);
        view.reset();
        resignAvailable = true;
        // Snapshot the TC settings used for THIS game so a later mid-game
        // edit can detect drift.
        try {
          const s = await ctx.api("GET", "/settings");
          gameTcInitial = Number(s.tc_initial_seconds);
          gameTcIncrement = Number(s.tc_increment_seconds);
        } catch {
          // ignore — drift detection just won't trigger for TC.
        }
        refreshButtons();
      } catch (e) {
        reportError(ctx, "New game failed", e);
      }
    };

    const onResign = async () => {
      const ok = await confirm({
        message: "Resign the current game?",
        okLabel: "Resign",
        cancelLabel: "Keep playing",
        destructive: true,
      });
      if (!ok) return;
      try {
        await ctx.api("POST", "/game/resign", {});
      } catch (e) {
        reportError(ctx, "Resign failed", e);
      }
    };

    const onSavePgn = async () => {
      const needsPause = !viewing && !paused && !gameOver && resignAvailable;
      if (needsPause) {
        try { await ctx.api("POST", "/game/pause", {}); } catch (e) {
          reportError(ctx, "Save PGN failed", e);
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
        reportError(ctx, "Save PGN failed", e);
      } finally {
        if (needsPause) {
          try { await ctx.api("POST", "/game/resume", {}); } catch (_) { /* best-effort */ }
        }
      }
    };

    let takebackPending = false;
    const onTakeback = async () => {
      if (takebackPending) return;
      takebackPending = true;
      try {
        await ctx.api("POST", "/game/takeback", {});
      } catch (e) {
        reportError(ctx, "Take-back failed", e);
      } finally {
        takebackPending = false;
      }
    };

    const onImport = async () => {
      if (!await _confirmDiscardActiveGame({
        message: "Cancel the current game and import another?",
        okLabel: "Import",
      })) return;
      // Dialog validates (parse errors surface inline) but does not import.
      const result = await showImportPositionDialog({ api: ctx.api });
      if (!result) return;
      // Same game already in view -- stay put, no re-import needed.
      if (viewing && result.hash && result.hash === _viewingHash) {
        if (viewingGameId) toast(`Viewing ${viewingGameId}`);
        return;
      }
      // Different game while viewing -- confirm before replacing.
      if (!await _confirmReplaceViewedGame({ incomingHash: result.hash, incomingSummary: result.summary })) return;
      try {
        closeAi();
        const r = await ctx.api("POST", "/game/import", { format: result.format, text: result.text });
        view.setGameId(r.game_id);
        ctx.api("POST", "/game/sync", {}).catch(() => {});
      } catch (e) {
        reportError(ctx, "Import failed", e);
      }
    };

    const onSwitchSides = async () => {
      try {
        await ctx.api("POST", "/game/switch-sides", {});
      } catch (e) {
        reportError(ctx, "Switch sides failed", e);
      }
    };

    const onPause = async () => {
      try {
        await ctx.api("POST", paused ? "/game/resume" : "/game/pause", {});
      } catch (e) {
        reportError(ctx, paused ? "Resume failed" : "Pause failed", e);
      }
    };

    // Parse FEN fields: side-to-move letter and a {wK,wQ,bK,bQ} castling map.
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

    // Server is authoritative for edit state. We start editing by POSTing
    // /game/edit/start; the resulting board_update flips `editing` true,
    // and we then enable the client-side board editor extension.
    function _onServerEditingStart() {
      const seed = _seedFromFen(view.getFen());
      view.enterEditMode(() => refreshButtons(), seed);
      pendingAnnotation = null;
      pushNavToUi();
      refreshButtons();
    }

    function _clearEditTransitionSuppression() {
      if (!suppressCommentsForEditTransition) return;
      suppressCommentsForEditTransition = false;
      syncCommentsVisibility();
    }

    function _onServerEditingStop() {
      view.exitEditMode();
      _clearEditTransitionSuppression();
      pendingAnnotation = null;
      pushNavToUi();
      refreshButtons();
    }

    async function _enterEditFromCurrentMode() {
      // Server requires view mode before edit. From play mode, flip into
      // view via /game/view/start (no recents write); /game/import would
      // pollute the recents history with the current play position.
      if (!viewing) {
        if (!await _confirmDiscardActiveGame({
          message: "Cancel the game in progress and edit the position?",
          okLabel: "Edit position",
        })) return;
        // Suppress the commentary dock for the duration of the transient
        // play->view->edit flip. Without this, syncCommentsVisibility
        // races view_last() and resets the cursor to 0.
        suppressCommentsForEditTransition = true;
        try {
          closeAi();
          const r = await ctx.api("POST", "/game/view/start", {});
          view.setGameId(r.game_id);
          await ctx.api("POST", "/game/sync", {});
        } catch (e) {
          _clearEditTransitionSuppression();
          reportError(ctx, "Edit position failed", e);
          return;
        }
      }
      if (analyzing) {
        const ok = await confirm({
          message: "Stop analysis and edit the position?",
          okLabel: "Edit position",
          cancelLabel: "Keep analyzing",
          destructive: true,
        });
        if (!ok) {
          _clearEditTransitionSuppression();
          return;
        }
      }
      try {
        closeAi();
        await ctx.api("POST", "/game/edit/start", {});
      } catch (e) {
        _clearEditTransitionSuppression();
        reportError(ctx, "Edit position failed", e);
      }
    }

    const onEditPosition = _enterEditFromCurrentMode;
    const onViewEditPosition = _enterEditFromCurrentMode;

    function _closeSidePopover() {
      editSidePopover.classList.add("hidden");
      editSideBtn.setAttribute("aria-expanded", "false");
    }
    const onEditSide = (ev) => {
      ev.stopPropagation();
      const isOpen = !editSidePopover.classList.contains("hidden");
      if (isOpen) {
        _closeSidePopover();
      } else {
        editSidePopover.classList.remove("hidden");
        editSideBtn.setAttribute("aria-expanded", "true");
      }
    };
    const onEditSideToggle = () => {
      view.setEditSide(view.getEditSide() === "w" ? "b" : "w");
      refreshButtons();
    };

    const onEditCastleCb = (right) => () => {
      view.toggleCastlingRight(right);
      refreshButtons();
    };

    function _closeCastlePopover() {
      editCastlePopover.classList.add("hidden");
      editCastleBtn.setAttribute("aria-expanded", "false");
    }
    const onEditCastleBtn = (ev) => {
      ev.stopPropagation();
      const isOpen = !editCastlePopover.classList.contains("hidden");
      if (isOpen) {
        _closeCastlePopover();
      } else {
        editCastlePopover.classList.remove("hidden");
        editCastleBtn.setAttribute("aria-expanded", "true");
      }
    };
    const onDocClickClosePopover = (ev) => {
      if (!editing) return;
      if (!editCastlePopover.classList.contains("hidden") &&
          !editCastlePopover.contains(ev.target) && !editCastleBtn.contains(ev.target)) {
        _closeCastlePopover();
      }
      if (!editSidePopover.classList.contains("hidden") &&
          !editSidePopover.contains(ev.target) && !editSideBtn.contains(ev.target)) {
        _closeSidePopover();
      }
    };

    const onEditAnnotate = async () => {
      // Preload from pendingAnnotation (if user already staged something
      // this edit session) or fall back to the server's current comment.
      const preload = pendingAnnotation ?? (lastViewComment ?? "");
      const result = await editAnnotation({ currentText: preload });
      if (result?.apply) {
        pendingAnnotation = result.text;
        // Optimistically reflect the staged text in the commentary dock
        // so the user sees their pending change. Lives until edit-commit
        // (server then makes it real) or edit-cancel (we restore the
        // pre-edit text from lastViewComment).
        if (isCommentaryOpen()) {
          setCommentaryText(pendingAnnotation || null);
        }
      }
    };

    const onEditConfirm = async () => {
      const fen = view.getEditFen();
      // Server mints a fresh game_id on a real position change. Clear the
      // filter so the board_update SSE (which races the POST response) isn't
      // dropped for not matching our stale id.
      view.setGameId(null);
      const payload = { fen };
      if (pendingAnnotation !== null) {
        payload.apply_comment = true;
        payload.comment_text = pendingAnnotation;
      }
      try {
        const r = await ctx.api("POST", "/game/edit/commit", payload);
        view.setGameId(r.game_id);
        // Annotation-only commit can promote an unsaved fork child to
        // recents (xgame nav "lazy commit"). Game_id is unchanged so
        // the board_update doesn't trigger fetchXgameInfo -- refetch
        // explicitly so the fork glyph + banner state catch up.
        if (r.game_id) fetchXgameInfo(r.game_id);
      } catch (e) {
        reportError(ctx, "Invalid position", e);
      }
    };

    const onEditCancel = async () => {
      try {
        const r = await ctx.api("POST", "/game/edit/cancel", {});
        if (r?.game_id) view.setGameId(r.game_id);
      } catch (e) {
        reportError(ctx, "Cancel edit failed", e);
      }
    };

    let commentNavPrev = null;
    let commentNavNext = null;

    // Single push of the gated nav state to the UI. Buttons are forced
    // null while analyzing or editing (view/goto is rejected in those
    // modes, so the targets would be unreachable anyway).
    const pushNavToUi = () => {
      const gated = analyzing || editing;
      setCommentaryNavState(gated ? null : commentNavPrev, gated ? null : commentNavNext);
    };

    // Tracks how the cursor reached the next ply: "precise" (back /
    // forward / goto / move-list click) vs "jump" (first / last). The
    // parent->child banner only fires on precise landings -- jumping
    // over a fork must NOT pop a prompt.
    let lastViewNavKind = "precise";
    async function doViewNav(endpoint, payload = {}) {
      lastViewNavKind =
        endpoint === "/game/view/first" || endpoint === "/game/view/last"
          ? "jump"
          : "precise";
      try {
        await ctx.api("POST", endpoint, payload);
      } catch (e) {
        reportError(ctx, "Navigation failed", e);
      }
    }

    const onViewNav = (endpoint) => () => doViewNav(endpoint);
    const onViewFlip = () => {
      viewFlipped = !viewFlipped;
      try { localStorage.setItem(VIEW_FLIP_KEY, viewFlipped ? "1" : "0"); } catch { /* */ }
      view.setHumanWhite(!viewFlipped);
    };

    const onViewFirst = onViewNav("/game/view/first");
    const onViewBack = onViewNav("/game/view/back");
    const onViewForward = onViewNav("/game/view/forward");
    const onViewLast = onViewNav("/game/view/last");

    setCommentaryNavHandlers(
      () => { if (commentNavPrev != null) doViewNav("/game/view/goto", { ply: commentNavPrev }); },
      () => { if (commentNavNext != null) doViewNav("/game/view/goto", { ply: commentNavNext }); },
    );

    let playFromHereInflight = false;
    const onPlayFromHere = async () => {
      if (playFromHereInflight) return;  // debounce double-click
      playFromHereInflight = true;
      setDisabled(viewPlayFromHereBtn, true);
      // Reset gameId so the racing board_update from new_game (which fires
      // BEFORE the API response carrying the new id) isn't dropped by the
      // game_id filter — that drop loses the human_white/name swap.
      view.setGameId(null);
      try {
        const playerName = getConfiguredPlayerName();
        view.setPlayerName(playerName);
        closeAi();
        const r = await ctx.api("POST", "/game/view/play-from-here", { player_name: playerName });
        view.setGameId(r.game_id);
        // Snapshot TC for drift detection (mirrors onNewGame).
        try {
          const s = await ctx.api("GET", "/settings");
          gameTcInitial = Number(s.tc_initial_seconds);
          gameTcIncrement = Number(s.tc_increment_seconds);
        } catch {
          // ignore
        }
      } catch (e) {
        reportError(ctx, "Play from here failed", e);
      } finally {
        playFromHereInflight = false;
        // Don't re-enable directly; refreshButtons() drives it next time
        // viewing flips, and by then the button is hidden anyway.
      }
    };

    // Show the persistent "Analysis mode on" toast. Called both from
    // onAnalyze (user toggle) and from the board_update handler so the
    // toast restores itself when the perspective remounts (navigate away
    // and back) and the server re-emits analyzing: true.
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

    function showAnalysisToast() {
      dismissAnalysisToast?.();
      const msg = document.createElement("span");
      msg.className = "toast-sort-msg";
      const label = document.createElement("span");
      label.className = "toast-grow is-active";
      label.textContent = "Analysis mode";
      msg.append(label);
      msg.append(makeToastIconBtn("table-list", "Search Lines", onPvTable));
      msg.append(makeToastIconBtn("terminal", "UCI log", onUciLog));
      const stopBtn = makeToastIconBtn("magnifying-glass-minus", "Stop analysis", onAnalyze);
      stopBtn.classList.add("is-active");
      msg.append(stopBtn);
      dismissAnalysisToast = toast(msg, {
        variant: "neutral",
        duration: 0,
      });
    }

    // Stop side of the analyze toggle, extracted so the AI-window
    // close handler can trigger the same flow (snapshot + endpoint +
    // toast + panels) as the toolbar Stop button.
    async function stopAnalysisFromUi() {
      if (!analyzing) return;
      snapshotViewAnalysisState();
      try {
        await ctx.api("POST", "/game/analysis/stop", {});
      } catch (e) {
        reportError(ctx, "Stop analysis failed", e);
        return;
      }
      aiTurnFinished = false;
      dismissAnalysisToast?.();
      dismissAnalysisToast = null;
      closeDebugWindowsPersist();
    }

    // POST start + restore panels + toast + open/reset AI panel. Shared
    // by the analyze toggle and the re-analyze button so the two paths
    // can't drift. openAi() before resetAi(): resetAi sets the spinner
    // and no-ops when the body is null.
    async function startAnalysisFromUi() {
      await ctx.api("POST", "/game/analysis/start", {});
      restoreViewAnalysisWindows(ctx.events);
      aiTurnFinished = false;
      showAnalysisToast();
      if (aiEnabled) {
        // Pin the title to the model that is actually about to run.
        // Mid-session provider/model edits do not retitle until the
        // user clicks Analyze (or the re-analyze button) again.
        setAiTitle(aiTitleModel);
        openAi();
        resetAi();
      }
    }

    const onAnalyze = async () => {
      if (analyzing) {
        await stopAnalysisFromUi();
        return;
      }
      try {
        await startAnalysisFromUi();
      } catch (e) {
        reportError(ctx, "Start analysis failed", e);
      }
    };

    // Re-analyze: stop the current turn server-side (if any), then start
    // a fresh one. Distinct from the Analyze toggle which closes on a
    // second click; this path keeps the AI panel open and mirrors the
    // snapshot/restore dance of stopAnalysisFromUi + onAnalyze so view
    // mode panels survive the round-trip. `reanalyzeInFlight` guards
    // against rapid double-clicks producing a spurious second start
    // that the server would reject with ModeConflictError.
    let reanalyzeInFlight = false;
    const onReanalyze = async () => {
      if (reanalyzeInFlight) return;
      reanalyzeInFlight = true;
      try {
        if (analyzing) {
          snapshotViewAnalysisState();
          await ctx.api("POST", "/game/analysis/stop", {});
        }
        await startAnalysisFromUi();
      } catch (e) {
        reportError(ctx, "Re-analyze failed", e);
      } finally {
        reanalyzeInFlight = false;
      }
    };
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

    function showEngineCrashToast() {
      const msg = document.createElement("span");
      msg.className = "toast-grow";
      msg.textContent = "Engine crashed unexpectedly.";
      const node = document.createElement("span");
      node.className = "toast-sort-msg";
      let dismissCrashToast;
      node.append(msg, makeToastDismissBtn(() => dismissCrashToast?.()));
      dismissCrashToast = toast(node, { variant: "danger", duration: 0 });
    }

    const offCrash = ctx.events.on(async (evt) => {
      if (evt.kind !== "system" || evt.payload?.error !== "engine_terminated") return;
      view.clearArrows();
      if (analyzing) {
        await onAnalyze();
      }
      showEngineCrashToast();
    });

    return {
      ready: Promise.resolve(),
      async canUnmount() {
        if (!editing) return true;
        return await confirm({
          message: "Leaving will cancel your position edit. Continue?",
          okLabel: "Leave",
          cancelLabel: "Stay",
          destructive: true,
        });
      },
      unmount() {
        if (editing) {
          // Fire-and-forget cancel so the server doesn't stay stuck in
          // edit mode if the user navigates away.
          ctx.api("POST", "/game/edit/cancel", {}).catch(() => {});
          view.exitEditMode();
        }
        closeDebugWindows();
        // Announce no active ribbon so the global float manager unmounts it.
        window.dispatchEvent(new CustomEvent("sturddle:ribbon-active", { detail: { el: null } }));
        setDockContainer(null);
        closeCommentary();
        setCommentaryDockContainer(null);
        setOnUserCloseCommentary(null);
        closeAi();
        setOnUserCloseAi(null);
        setOnReanalyzeAi(null);
        dismissAnalysisToast?.();
        dismissAnalysisToast = null;
        dismissGameOverToast?.();
        dismissGameOverToast = null;
        pausedBadge?.classList.add("hidden");
        showFinishedBadge("");
        offCrash();
        offEvent();
        view.unmount();
        // Close any live x-game toasts so they don't outlive the
        // perspective. Plain close (not via the X handler), so the
        // dismiss flags are NOT set -- if the user returns to the
        // play perspective at the same fork ply, the toast re-fires.
        closeXgameToasts();
        // Lock class lives on <body>; clear it so it can't outlive the
        // perspective if we unmount mid-analysis.
        document.body.classList.remove(XGAME_LOCK_CLASS);
        window.removeEventListener("sturddle:settings-changed", onSettingsChanged);
        window.removeEventListener("sturddle:layout-changed", onLayoutChanged);
        window.removeEventListener("sturddle:engines-changed", onEnginesChanged);
        window.removeEventListener("sturddle:recents-changed", onRecentsChanged);
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
