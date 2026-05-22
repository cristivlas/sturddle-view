// Play perspective: human vs engine.
// Composes the reusable GameView with a game-control bar (New / Take back /
// Resign).

import { mountGameView } from "../game-view.js";
import { alert as showAlert, confirm, openSettings, reportError, toast } from "../dialogs.js";
import { showImportPositionDialog, confirmReplaceViewedGame, confirmDiscardViewedGame } from "../import-position-dialog.js";
import { toggleUciLogWindow, togglePvTableWindow, closeDebugWindows, closeDebugWindowsPersist, restoreDebugWindows, snapshotViewAnalysisState, restoreViewAnalysisWindows, setDockContainer, isMobileLayout } from "../play-debug-windows.js";
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
import { terminationLabel } from "../format-termination.js";
import { editAnnotation } from "../annotation-dialog.js";

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
              <wa-icon name="magnifying-glass"></wa-icon>
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
              <wa-icon name="magnifying-glass"></wa-icon>
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

    // Visible whenever the server has no engine configured. Hides the
    // "no engine" failure mode behind a single visible CTA instead of
    // waiting for the user to click New game and see an error toast.
    function setNoEngine(noEngine) {
      noEngineBanner.classList.toggle("hidden", !noEngine);
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
      onMoveJump: (plyIndex) => { if (!analyzing) doViewNav("/game/view/goto", { ply: plyIndex + 1 }); },
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
        const wasOpen = open;
        if (!open) openCommentary();
        setCommentaryText(lastViewComment);
        // Skip the seeding /view/goto in edit mode: view_goto is FSM-gated
        // to VIEWING, not EDITING, and would 400. Nav state stays whatever
        // it was before edit; refreshes on exit when this re-runs.
        // Alternative considered: hide the commentary dock entirely while
        // editing (parallel to the play->edit transient-suppress flag
        // documented at suppressCommentsForEditTransition, see commit
        // 7158076). Rejected: too aggressive -- user loses passive view
        // of the comment they're about to annotate.
        if (!wasOpen && !analyzing && !editing) {
          // Populate comment nav state on first open.
          doViewNav("/game/view/goto", { ply: viewCursor });
        }
      } else if (open) {
        commentNavPrev = null;
        commentNavNext = null;
        closeCommentary();
      }
    }
    const onCommentsResize = () => { syncCommentsVisibility(); };
    window.addEventListener("resize", onCommentsResize);
    // Snapshot of TC fields used at the start of the current game; lets
    // us tell the user "applies on next game" if they edit TC mid-play.
    let gameTcInitial = null;
    let gameTcIncrement = null;
    async function refreshSettings({ notifyOnDrift = false } = {}) {
      try {
        const s = await ctx.api("GET", "/settings");
        allowTakeback = s.allow_takeback !== false;
        showPgnComments = s.view_show_pgn_comments !== false;
        syncCommentsVisibility();
        if (notifyOnDrift && !gameOver && resignAvailable) {
          const drift = [];
          // Side: settings.human_side is "white"|"black"|"random". Only
          // compare deterministic choices; "random" never conflicts.
          if (s.human_side === "white" && humanWhite === false) drift.push("side");
          else if (s.human_side === "black" && humanWhite === true) drift.push("side");
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
    // Single sync point: every analyzing write goes through this setter so
    // the module-scope mirror (_analyzing) used by isAnalyzing() stays
    // current. Direct `analyzing = ...` writes will drift -- always call
    // setAnalyzing instead.
    function setAnalyzing(v) {
      analyzing = !!v;
      _analyzing = analyzing;
    }
    // View mode state (set from board_update.view payload).
    let viewing = false;
    let viewCursor = 0;
    let viewTotalPlies = 0;
    let viewGameOver = false;
    let viewGameOverAlertShown = false;
    let viewingGameId = null;
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

    const pauseIcon = pauseBtn.querySelector("wa-icon");
    // Resign is enabled whenever there is an active game; cleared on
    // game_result. We track it explicitly so paused-state can additionally
    // gate it without losing the "active game" signal.
    let resignAvailable = false;
    function setDisabled(btn, disabled) {
      if (disabled) btn.setAttribute("disabled", "");
      else btn.removeAttribute("disabled");
    }
    function refreshButtons() {
      // Swap ribbons: edit overrides view, which overrides play.
      playRibbon.style.display = (viewing || editing) ? "none" : "";
      viewRibbon.style.display = (viewing && !editing) ? "" : "none";
      editRibbon.style.display = editing ? "" : "none";
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
        setDisabled(viewFirstBtn, analyzing || atStart);
        setDisabled(viewBackBtn, analyzing || atStart);
        setDisabled(viewForwardBtn, analyzing || atEnd);
        setDisabled(viewLastBtn, analyzing || atEnd);
        setDisabled(viewSavePgnBtn, viewTotalPlies === 0);
        // Play-from-here is rejected at game-over plies (checkmate /
        // stalemate / draw). Backed by a backend guard that prevents
        // half-cleared state if the UI is bypassed.
        setDisabled(viewPlayFromHereBtn, analyzing || viewGameOver);
        viewAnalyzeBtn.classList.toggle("is-active", analyzing);
        viewAnalyzeBtn.setAttribute(
          "aria-label", analyzing ? "Stop analysis" : "Analysis mode",
        );
        viewAnalyzeBtn.setAttribute(
          "title", analyzing ? "Stop analysis" : "Analysis mode",
        );
        viewAnalyzeBtn.querySelector("wa-icon").setAttribute(
          "name", analyzing ? "circle-stop" : "magnifying-glass",
        );
        return;
      }
      const humanToMove = humanWhite ? turn === "white" : turn === "black";
      // Pause is restricted to the human's turn; Resume (paused=true) is
      // always allowed so a game paused on the engine's turn — e.g. after
      // exiting Analysis — can be unpaused.
      setDisabled(pauseBtn, gameOver || analyzing || (!paused && !humanToMove));
      pauseIcon.setAttribute("name", paused ? "forward-step" : "pause");
      pauseBtn.setAttribute("aria-label", paused ? "Resume" : "Pause");
      pauseBtn.setAttribute("title", paused ? "Resume" : "Pause");
      setDisabled(
        takebackBtn,
        analyzing || gameOver || !allowTakeback || movesPlayed === 0,
      );
      setDisabled(savePgnBtn, movesPlayed === 0);
      setDisabled(switchSidesBtn, analyzing || gameOver || !resignAvailable);
      setDisabled(resignBtn, paused || analyzing || gameOver || !resignAvailable);
      // Analysis is reachable only from a paused game (and to stop, while
      // analyzing). Eliminates the "pause + enter analysis" combined step.
      setDisabled(
        analyzeBtn,
        gameOver || !resignAvailable || (!analyzing && !paused),
      );
      analyzeBtn.classList.toggle("is-active", analyzing);
      analyzeBtn.setAttribute(
        "aria-label",
        analyzing ? "Stop analysis" : "Analysis mode",
      );
      analyzeBtn.setAttribute(
        "title",
        analyzing ? "Stop analysis" : "Analysis mode",
      );
      analyzeBtn.querySelector("wa-icon").setAttribute(
        "name", analyzing ? "circle-stop" : "magnifying-glass",
      );
    }

    // View-mode flip is purely visual (no backend state; the user isn't
    // playing yet so "which side am I" is meaningless). Persisted so it
    // survives perspective remounts; applied on each entry into view mode.
    const VIEW_FLIP_KEY = "sturddle:view:flipped";
    let viewFlipped = false;
    try { viewFlipped = localStorage.getItem(VIEW_FLIP_KEY) === "1"; } catch { /* */ }

    // --- Hook events for control-bar state changes (board state changes
    //     are GameView's responsibility). ---
    const offEvent = ctx.events.on((evt) => {
      switch (evt.kind) {
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
            if (!wasViewing || viewingGameId !== prevGameId) viewGameOverAlertShown = false;
            viewCursor = v.cursor ?? 0;
            viewTotalPlies = v.total_plies ?? 0;
            viewGameOver = !!v.game_over;
            lastViewComment = v.comment ?? null;
            _viewingHash = v.view_hash ?? null;
            _viewingSummary = v.view_summary ?? null;
            _viewing = true;
            syncCommentsVisibility();
            if (v.result) showFinishedBadge(resultBadge(v.result));
            if (viewGameOver && viewCursor === viewTotalPlies && v.result && !viewGameOverAlertShown) {
              viewGameOverAlertShown = true;
              toast(formatViewGameOver(v), { variant: "neutral", duration: 6000 });
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
            syncCommentsVisibility();
            if (wasViewing) restoreDebugWindows(ctx.events);
            resignAvailable = true;
          }
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
            setCommentaryNavState(analyzing ? null : commentNavPrev, analyzing ? null : commentNavNext);
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
        view.setGameId(null);
        const r = await ctx.api("POST", "/game/new", {});
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
        const blob = await r.blob();
        const cd = r.headers.get("Content-Disposition") || "";
        const match = cd.match(/filename="([^"]+)"/);
        const filename = match ? match[1] : "game.pgn";
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
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

    async function doViewNav(endpoint, payload = {}) {
      try {
        const body = isCommentaryOpen()
          ? { ...payload, include_comment_nav: true }
          : payload;
        const res = await ctx.api("POST", endpoint, body);
        if (isCommentaryOpen() && "prev_comment" in res) {
          commentNavPrev = res.prev_comment ?? null;
          commentNavNext = res.next_comment ?? null;
          setCommentaryNavState(analyzing ? null : commentNavPrev, analyzing ? null : commentNavNext);
        }
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
        const r = await ctx.api("POST", "/game/view/play-from-here", {});
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
      msg.style.display = "flex";
      msg.style.alignItems = "center";
      msg.style.gap = "6px";
      msg.append("Analysis mode");
      const pvBtn = makeToastIconBtn("table-list", "Search Lines", onPvTable);
      pvBtn.style.marginLeft = "auto";
      msg.append(pvBtn);
      msg.append(makeToastIconBtn("terminal", "UCI log", onUciLog));
      msg.append(makeToastIconBtn("circle-stop", "Stop analysis", onAnalyze));
      dismissAnalysisToast = toast(msg, {
        variant: "neutral",
        duration: 0,
      });
    }

    const onAnalyze = async () => {
      const wasAnalyzing = analyzing;
      if (wasAnalyzing) snapshotViewAnalysisState();
      try {
        await ctx.api(
          "POST",
          wasAnalyzing ? "/game/analysis/stop" : "/game/analysis/start",
          {},
        );
        if (wasAnalyzing) {
          dismissAnalysisToast?.();
          dismissAnalysisToast = null;
        } else {
          restoreViewAnalysisWindows(ctx.events);
          showAnalysisToast();
        }
      } catch (e) {
        reportError(
          ctx,
          wasAnalyzing ? "Stop analysis failed" : "Start analysis failed",
          e,
        );
      }
    };

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
      msg.textContent = "Engine crashed unexpectedly.";
      const closeBtn = document.createElement("button");
      closeBtn.className = "toast-action-btn toast-close-btn";
      closeBtn.textContent = "X";
      const node = document.createElement("span");
      node.className = "toast-sort-msg";
      closeBtn.style.marginLeft = "auto";
      node.append(msg, closeBtn);
      const dismiss = toast(node, { variant: "danger", duration: 0 });
      closeBtn.onclick = dismiss;
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
        setDockContainer(null);
        closeCommentary();
        setCommentaryDockContainer(null);
        setOnUserCloseCommentary(null);
        dismissAnalysisToast?.();
        dismissAnalysisToast = null;
        pausedBadge?.classList.add("hidden");
        showFinishedBadge("");
        offCrash();
        offEvent();
        view.unmount();
        window.removeEventListener("sturddle:settings-changed", onSettingsChanged);
        window.removeEventListener("sturddle:engines-changed", onEnginesChanged);
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
