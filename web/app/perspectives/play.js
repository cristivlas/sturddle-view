// Play perspective: human vs engine.
// Composes the reusable GameView with a game-control bar (New / Take back /
// Resign).

import { mountGameView } from "../game-view.js";
import { alert as showAlert, confirm, reportError, toast } from "../dialogs.js";
import { showImportPositionDialog } from "../import-position-dialog.js";

// Reduce a game_result payload to the canonical chess result string for
// the header badge. resign/timeout don't carry "1-0"/"0-1" in the payload
// so we derive it from who lost (only human can resign today).
function formatResult(payload, humanWhite) {
  const { result, by, loser } = payload;
  if (result === "1-0" || result === "0-1") return result;
  if (result === "1/2-1/2") return "½-½";
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

function formatGameOver(payload, humanWhite) {
  const { result, termination, by, loser } = payload;
  if (result === "resign") {
    return by === "human" ? "You resigned." : "Engine resigned.";
  }
  if (result === "timeout") {
    const humanLost = (loser === "white") === humanWhite;
    return humanLost ? "You lost on time." : "Engine lost on time.";
  }
  // Standard chess result string + python-chess termination name.
  // Map known terminations to a short phrase; fall back to the raw name.
  const reasons = {
    checkmate: "Checkmate",
    stalemate: "Stalemate",
    insufficient_material: "Draw — insufficient material",
    seventyfive_moves: "Draw — 75-move rule",
    fivefold_repetition: "Draw — fivefold repetition",
    fifty_moves: "Draw — 50-move rule",
    threefold_repetition: "Draw — threefold repetition",
  };
  const reason = reasons[termination] ?? (termination ?? "Game over");
  if (result === "1-0" || result === "0-1") {
    const humanWon = (result === "1-0") === humanWhite;
    return `${reason} — ${humanWon ? "you win" : "engine wins"}.`;
  }
  // 1/2-1/2 or unknown.
  return reason;
}

export const playPerspective = {
  id: "play",
  label: "Play",

  async mount(root, ctx) {
    root.innerHTML = `
      <section id="play-perspective">
        <div class="play-grid">
          <div class="play-board-host"></div>

          <div id="board-controls" class="board-ribbon">
            <button id="new-game" class="ribbon-btn" aria-label="New game" title="New game">
              <wa-icon name="plus"></wa-icon>
            </button>
            <button id="import-pos" class="ribbon-btn desktop-only" aria-label="Open position from FEN or PGN" title="Open">
              <wa-icon name="folder-open"></wa-icon>
            </button>
            <span class="ribbon-sep" aria-hidden="true"></span>
            <button id="takeback" class="ribbon-btn" disabled aria-label="Take back" title="Take back">
              <wa-icon name="rotate-left"></wa-icon>
            </button>
            <button id="switch-sides" class="ribbon-btn" disabled aria-label="Switch sides" title="Flip board">
              <wa-icon name="arrow-right-arrow-left"></wa-icon>
            </button>
            <span class="ribbon-sep" aria-hidden="true"></span>
            <button id="pause" class="ribbon-btn" disabled aria-label="Pause" title="Pause">
              <wa-icon name="pause"></wa-icon>
            </button>
            <button id="analyze" class="ribbon-btn" disabled aria-label="Analysis mode" title="Analysis mode">
              <wa-icon name="magnifying-glass"></wa-icon>
            </button>
            <button id="resign" class="ribbon-btn ribbon-btn--danger" disabled aria-label="Resign" title="Resign">
              <wa-icon name="flag"></wa-icon>
            </button>
          </div>

          <div id="view-controls" class="board-ribbon" style="display: none">
            <button id="view-import" class="ribbon-btn desktop-only" aria-label="Open another position" title="Open">
              <wa-icon name="folder-open"></wa-icon>
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
            <button id="view-flip" class="ribbon-btn" aria-label="Flip board" title="Flip board">
              <wa-icon name="arrow-right-arrow-left"></wa-icon>
            </button>
            <button id="view-analyze" class="ribbon-btn" aria-label="Analysis mode" title="Analysis mode">
              <wa-icon name="magnifying-glass"></wa-icon>
            </button>
            <button id="view-play-from-here" class="ribbon-btn" aria-label="Play from here" title="Play from here">
              <wa-icon name="play"></wa-icon>
            </button>
          </div>

          <div class="play-side-host"></div>
        </div>
      </section>
    `;

    const boardHost = root.querySelector(".play-board-host");
    const sideHost = root.querySelector(".play-side-host");
    boardHost.classList.add("board-idle");
    const newGameBtn = root.querySelector("#new-game");
    const importBtn = root.querySelector("#import-pos");
    const resignBtn = root.querySelector("#resign");
    const takebackBtn = root.querySelector("#takeback");
    const switchSidesBtn = root.querySelector("#switch-sides");
    const pauseBtn = root.querySelector("#pause");
    const analyzeBtn = root.querySelector("#analyze");
    // View ribbon (shown only while a game is loaded into view mode).
    const playRibbon = root.querySelector("#board-controls");
    const viewRibbon = root.querySelector("#view-controls");
    const viewImportBtn = root.querySelector("#view-import");
    const viewFirstBtn = root.querySelector("#view-first");
    const viewBackBtn = root.querySelector("#view-back");
    const viewForwardBtn = root.querySelector("#view-forward");
    const viewLastBtn = root.querySelector("#view-last");
    const viewFlipBtn = root.querySelector("#view-flip");
    const viewAnalyzeBtn = root.querySelector("#view-analyze");
    const viewPlayFromHereBtn = root.querySelector("#view-play-from-here");

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
    });

    // Settings cache (refreshed on settings-changed).
    let allowTakeback = true;
    // Snapshot of TC fields used at the start of the current game; lets
    // us tell the user "applies on next game" if they edit TC mid-play.
    let gameTcInitial = null;
    let gameTcIncrement = null;
    async function refreshSettings({ notifyOnDrift = false } = {}) {
      try {
        const s = await ctx.api("GET", "/settings");
        allowTakeback = s.allow_takeback !== false;
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
    // Delay slightly so the GameView's first recompute and board mount have
    // settled before we apply the snapshot.
    setTimeout(() => {
      ctx.api("POST", "/game/sync", {}).catch(() => {});
    }, 200);

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
    // View mode state (set from board_update.view payload).
    let viewing = false;
    let viewCursor = 0;
    let viewTotalPlies = 0;
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
      // Swap ribbons based on mode. View ribbon is visible only when the
      // backend reports viewing=true; play ribbon takes back over after
      // play_from_here.
      playRibbon.style.display = viewing ? "none" : "";
      viewRibbon.style.display = viewing ? "" : "none";
      if (viewing) {
        const atStart = viewCursor === 0;
        const atEnd = viewCursor === viewTotalPlies;
        setDisabled(viewFirstBtn, analyzing || atStart);
        setDisabled(viewBackBtn, analyzing || atStart);
        setDisabled(viewForwardBtn, analyzing || atEnd);
        setDisabled(viewLastBtn, analyzing || atEnd);
        setDisabled(viewPlayFromHereBtn, analyzing);
        viewAnalyzeBtn.classList.toggle("is-active", analyzing);
        viewAnalyzeBtn.setAttribute(
          "aria-label", analyzing ? "Stop analysis" : "Analysis mode",
        );
        viewAnalyzeBtn.setAttribute(
          "title", analyzing ? "Stop analysis" : "Analysis mode",
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
        paused || analyzing || gameOver || !allowTakeback || movesPlayed === 0,
      );
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
    }

    // --- Hook events for control-bar state changes (board state changes
    //     are GameView's responsibility). ---
    const offEvent = ctx.events.on((evt) => {
      switch (evt.kind) {
        case "board_update": {
          movesPlayed = evt.payload.moves_san?.length ?? 0;
          gameOver = false;
          showFinishedBadge("");
          // View mode swaps the ribbon and suppresses play-mode signals
          // (resignAvailable, etc.) — the user isn't playing yet.
          const v = evt.payload.view;
          viewing = !!v;
          if (viewing) {
            viewCursor = v.cursor ?? 0;
            viewTotalPlies = v.total_plies ?? 0;
            resignAvailable = false;
            // Board is read-only in view mode; the user navigates via ribbon.
            view.setEnabled(false);
          } else {
            resignAvailable = true;
          }
          if (typeof evt.payload.human_white === "boolean") {
            humanWhite = evt.payload.human_white;
          }
          if (evt.payload.turn) turn = evt.payload.turn;
          if (typeof evt.payload.analyzing === "boolean") {
            analyzing = evt.payload.analyzing;
            // Don't re-enable interactivity in view mode regardless of
            // analysis state.
            if (!viewing) view.setEnabled(!analyzing && !paused);
            syncPausedUi();
            if (!analyzing) {
              dismissAnalysisToast?.();
              dismissAnalysisToast = null;
            }
          }
          boardHost.classList.remove("board-idle");
          setDisabled(newGameBtn, false);
          refreshButtons();
          break;
        }
        case "game_result":
          gameOver = true;
          paused = false;
          analyzing = false;
          dismissAnalysisToast?.();
          dismissAnalysisToast = null;
          resignAvailable = false;
          setDisabled(newGameBtn, false);
          boardHost.classList.add("board-idle");
          syncPausedUi();
          showFinishedBadge(formatResult(evt.payload, humanWhite));
          refreshButtons();
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

    const onNewGame = async () => {
      if (movesPlayed > 0 && !gameOver) {
        const ok = await confirm({
          message: "Cancel the game in progress and start a new one?",
          okLabel: "New game",
          cancelLabel: "Keep playing",
          destructive: true,
        });
        if (!ok) return;
      }
      try {
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

    const onTakeback = async () => {
      try {
        await ctx.api("POST", "/game/takeback", {});
      } catch (e) {
        reportError(ctx, "Take-back failed", e);
      }
    };

    const onImport = async () => {
      if (movesPlayed > 0 && !gameOver && !viewing) {
        const ok = await confirm({
          message: "Cancel the game in progress and import a new position?",
          okLabel: "Import",
          cancelLabel: "Keep playing",
          destructive: true,
        });
        if (!ok) return;
      }
      const result = await showImportPositionDialog({ api: ctx.api });
      if (!result) return;
      try {
        const r = await ctx.api("POST", "/game/import", result);
        view.setGameId(r.game_id);
        // Import lands in view mode; the board_update event drives the
        // ribbon swap and disables interactivity. Sync to fetch the
        // imported board state.
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

    const onViewNav = (endpoint) => async () => {
      try {
        await ctx.api("POST", endpoint, {});
      } catch (e) {
        reportError(ctx, "Navigation failed", e);
      }
    };
    // View-mode flip is purely visual (no backend state; the user isn't
    // playing yet so "which side am I" is meaningless). Toggles board
    // orientation via the same setter onPlayFromHere uses.
    let viewFlipped = false;
    const onViewFlip = () => {
      viewFlipped = !viewFlipped;
      view.setHumanWhite(!viewFlipped);
    };

    const onViewFirst = onViewNav("/game/view/first");
    const onViewBack = onViewNav("/game/view/back");
    const onViewForward = onViewNav("/game/view/forward");
    const onViewLast = onViewNav("/game/view/last");

    const onPlayFromHere = async () => {
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
      }
    };

    const onAnalyze = async () => {
      const wasAnalyzing = analyzing;
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
          dismissAnalysisToast?.();
          const msg = document.createElement("span");
          msg.style.display = "inline-flex";
          msg.style.alignItems = "center";
          msg.style.gap = "6px";
          msg.append("Analysis mode on — ");
          const stopBtn = document.createElement("button");
          stopBtn.type = "button";
          stopBtn.className = "toast-icon-btn";
          stopBtn.setAttribute("aria-label", "Stop analysis");
          stopBtn.setAttribute("title", "Stop analysis");
          const ic = document.createElement("wa-icon");
          ic.setAttribute("name", "magnifying-glass");
          stopBtn.appendChild(ic);
          stopBtn.addEventListener("click", onAnalyze);
          msg.append(stopBtn, " to stop.");
          dismissAnalysisToast = toast(msg, {
            variant: "neutral",
            duration: 0,
          });
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
    resignBtn.addEventListener("click", onResign);
    takebackBtn.addEventListener("click", onTakeback);
    switchSidesBtn.addEventListener("click", onSwitchSides);
    pauseBtn.addEventListener("click", onPause);
    analyzeBtn.addEventListener("click", onAnalyze);
    viewImportBtn.addEventListener("click", onImport);
    viewFirstBtn.addEventListener("click", onViewFirst);
    viewBackBtn.addEventListener("click", onViewBack);
    viewForwardBtn.addEventListener("click", onViewForward);
    viewLastBtn.addEventListener("click", onViewLast);
    viewFlipBtn.addEventListener("click", onViewFlip);
    viewAnalyzeBtn.addEventListener("click", onAnalyze);
    viewPlayFromHereBtn.addEventListener("click", onPlayFromHere);

    return {
      unmount() {
        dismissAnalysisToast?.();
        dismissAnalysisToast = null;
        pausedBadge?.classList.add("hidden");
        showFinishedBadge("");
        offEvent();
        view.unmount();
        window.removeEventListener("sturddle:settings-changed", onSettingsChanged);
        window.removeEventListener("keydown", onKeydown);
        newGameBtn.removeEventListener("click", onNewGame);
        importBtn.removeEventListener("click", onImport);
        resignBtn.removeEventListener("click", onResign);
        takebackBtn.removeEventListener("click", onTakeback);
        switchSidesBtn.removeEventListener("click", onSwitchSides);
        pauseBtn.removeEventListener("click", onPause);
        analyzeBtn.removeEventListener("click", onAnalyze);
        viewImportBtn.removeEventListener("click", onImport);
        viewFirstBtn.removeEventListener("click", onViewFirst);
        viewBackBtn.removeEventListener("click", onViewBack);
        viewForwardBtn.removeEventListener("click", onViewForward);
        viewLastBtn.removeEventListener("click", onViewLast);
        viewFlipBtn.removeEventListener("click", onViewFlip);
        viewAnalyzeBtn.removeEventListener("click", onAnalyze);
        viewPlayFromHereBtn.removeEventListener("click", onPlayFromHere);
      },
    };
  },
};
