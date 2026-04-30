// Play perspective: human vs engine.
// Composes the reusable GameView with a game-control bar (New / Take back /
// Resign).

import { mountGameView } from "../game-view.js";
import { confirm, reportError, toast } from "../dialogs.js";

export const playPerspective = {
  id: "play",
  label: "Play",

  async mount(root, ctx) {
    root.innerHTML = `
      <section id="play-perspective">
        <div class="play-board-host"></div>

        <div id="board-controls">
          <wa-button id="new-game" size="small" variant="brand">New game</wa-button>
          <wa-button id="takeback" size="small" appearance="outlined" disabled>Take back</wa-button>
          <wa-button id="pause" class="icon-only" size="small" appearance="outlined" disabled aria-label="Pause">
            <wa-icon name="pause"></wa-icon>
          </wa-button>
          <wa-button id="resign" size="small" variant="danger" appearance="outlined" disabled>Resign</wa-button>
        </div>

        <div class="play-side-host"></div>
      </section>
    `;

    const boardHost = root.querySelector(".play-board-host");
    const sideHost = root.querySelector(".play-side-host");
    boardHost.classList.add("board-idle");
    const newGameBtn = root.querySelector("#new-game");
    const resignBtn = root.querySelector("#resign");
    const takebackBtn = root.querySelector("#takeback");
    const pauseBtn = root.querySelector("#pause");

    // --- GameView: board host on top, side host (moves+engine) below. ---
    const view = mountGameView(boardHost, {
      events: ctx.events,
      interactive: true,
      sideContainer: sideHost,
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
    const onSettingsChanged = () => { refreshSettings({ notifyOnDrift: true }); };
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
      const humanToMove = humanWhite ? turn === "white" : turn === "black";
      setDisabled(pauseBtn, gameOver || !humanToMove);
      pauseIcon.setAttribute("name", paused ? "play" : "pause");
      pauseBtn.setAttribute("aria-label", paused ? "Resume" : "Pause");
      setDisabled(
        takebackBtn,
        paused || gameOver || !allowTakeback || movesPlayed === 0,
      );
      setDisabled(resignBtn, paused || gameOver || !resignAvailable);
    }

    // --- Hook events for control-bar state changes (board state changes
    //     are GameView's responsibility). ---
    const offEvent = ctx.events.on((evt) => {
      switch (evt.kind) {
        case "board_update": {
          movesPlayed = evt.payload.moves_san?.length ?? 0;
          gameOver = false;
          resignAvailable = true;
          if (typeof evt.payload.human_white === "boolean") {
            humanWhite = evt.payload.human_white;
          }
          if (evt.payload.turn) turn = evt.payload.turn;
          boardHost.classList.remove("board-idle");
          // Disable New Game only when human-as-white is at startpos and
          // can simply make their first move to start play. Black-to-play
          // humans need the button to trigger the engine's first move.
          const idleAsWhite =
            movesPlayed === 0 && evt.payload.human_white === true;
          setDisabled(newGameBtn, idleAsWhite);
          refreshButtons();
          break;
        }
        case "game_result":
          gameOver = true;
          paused = false;
          resignAvailable = false;
          setDisabled(newGameBtn, false);
          boardHost.classList.add("board-idle");
          refreshButtons();
          toast(`Game over: ${evt.payload.result}`, { variant: "neutral" });
          break;
        case "clock_tick":
          if (typeof evt.payload.paused === "boolean" && evt.payload.paused !== paused) {
            paused = evt.payload.paused;
            view.setEnabled(!paused);
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

    const onPause = async () => {
      try {
        await ctx.api("POST", paused ? "/game/resume" : "/game/pause", {});
      } catch (e) {
        reportError(ctx, paused ? "Resume failed" : "Pause failed", e);
      }
    };

    newGameBtn.addEventListener("click", onNewGame);
    resignBtn.addEventListener("click", onResign);
    takebackBtn.addEventListener("click", onTakeback);
    pauseBtn.addEventListener("click", onPause);

    return {
      unmount() {
        offEvent();
        view.unmount();
        window.removeEventListener("sturddle:settings-changed", onSettingsChanged);
        newGameBtn.removeEventListener("click", onNewGame);
        resignBtn.removeEventListener("click", onResign);
        takebackBtn.removeEventListener("click", onTakeback);
        pauseBtn.removeEventListener("click", onPause);
      },
    };
  },
};
