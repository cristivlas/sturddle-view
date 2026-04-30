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
    async function refreshSettings() {
      try {
        const s = await ctx.api("GET", "/settings");
        allowTakeback = s.allow_takeback !== false;
      } catch {
        // ignore
      }
    }
    await refreshSettings();
    const onSettingsChanged = () => { refreshSettings(); };
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
    function refreshPauseBtn() {
      const humanToMove = humanWhite ? turn === "white" : turn === "black";
      const enable = !gameOver && humanToMove;
      if (enable) pauseBtn.removeAttribute("disabled");
      else pauseBtn.setAttribute("disabled", "");
      pauseIcon.setAttribute("name", paused ? "play" : "pause");
      pauseBtn.setAttribute("aria-label", paused ? "Resume" : "Pause");
    }

    // --- Hook events for control-bar state changes (board state changes
    //     are GameView's responsibility). ---
    const offEvent = ctx.events.on((evt) => {
      switch (evt.kind) {
        case "board_update": {
          movesPlayed = evt.payload.moves_san?.length ?? 0;
          gameOver = false;
          if (typeof evt.payload.human_white === "boolean") {
            humanWhite = evt.payload.human_white;
          }
          if (evt.payload.turn) turn = evt.payload.turn;
          refreshPauseBtn();
          boardHost.classList.remove("board-idle");
          const tbDis = !allowTakeback || movesPlayed === 0;
          if (tbDis) takebackBtn.setAttribute("disabled", "");
          else takebackBtn.removeAttribute("disabled");
          // Disable New Game only when human-as-white is at startpos and
          // can simply make their first move to start play. Black-to-play
          // humans need the button to trigger the engine's first move.
          const idleAsWhite =
            movesPlayed === 0 && evt.payload.human_white === true;
          if (idleAsWhite) newGameBtn.setAttribute("disabled", "");
          else newGameBtn.removeAttribute("disabled");
          break;
        }
        case "game_result":
          gameOver = true;
          paused = false;
          resignBtn.setAttribute("disabled", "");
          newGameBtn.removeAttribute("disabled");
          boardHost.classList.add("board-idle");
          refreshPauseBtn();
          toast(`Game over: ${evt.payload.result}`, { variant: "neutral" });
          break;
        case "clock_tick":
          if (typeof evt.payload.paused === "boolean" && evt.payload.paused !== paused) {
            paused = evt.payload.paused;
            view.setEnabled(!paused);
            refreshPauseBtn();
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
        resignBtn.removeAttribute("disabled");
      } catch (e) {
        reportError(ctx, "New game failed", e);
      }
    };

    const onResign = async () => {
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
