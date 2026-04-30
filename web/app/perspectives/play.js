// Play perspective: human vs engine.
// Composes the reusable GameView with a game-control bar (New / Take back /
// Resign) and a Debug toggle.

import { mountGameView } from "../game-view.js";
import { reportError, toast } from "../dialogs.js";

export const playPerspective = {
  id: "play",
  label: "Play",

  async mount(root, ctx) {
    root.innerHTML = `
      <section id="play-perspective">
        <div class="play-game-host"></div>

        <div id="board-controls">
          <wa-button id="new-game" size="small" variant="brand">New game</wa-button>
          <wa-button id="takeback" size="small" appearance="outlined" disabled>Take back</wa-button>
          <wa-button id="resign" size="small" variant="danger" appearance="outlined" disabled>Resign</wa-button>
          <span class="grow"></span>
          <label class="debug-toggle">
            <wa-switch id="show-debug" size="small"></wa-switch>
            <span>Debug</span>
          </label>
        </div>

        <div class="debug-only" hidden>
          <h2>Event log</h2>
          <pre id="event-log"></pre>
        </div>
      </section>
    `;

    const gameHost = root.querySelector(".play-game-host");
    const eventLogEl = root.querySelector("#event-log");
    const debugRoot = root.querySelector(".debug-only");
    const showDebug = root.querySelector("#show-debug");
    const newGameBtn = root.querySelector("#new-game");
    const resignBtn = root.querySelector("#resign");
    const takebackBtn = root.querySelector("#takeback");

    showDebug.addEventListener("change", () => {
      debugRoot.hidden = !showDebug.checked;
    });

    function append(el, text, max = 200) {
      el.textContent = (el.textContent + text + "\n").split("\n").slice(-max).join("\n");
    }

    // --- GameView (the visual): board, clocks, move list, engine info. ---
    const view = mountGameView(gameHost, {
      events: ctx.events,
      interactive: true,
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

    // --- Hook events for control-bar state changes (board state changes
    //     are GameView's responsibility). ---
    const offEvent = ctx.events.on((evt) => {
      switch (evt.kind) {
        case "board_update": {
          const dis =
            !allowTakeback || (evt.payload.moves_san?.length ?? 0) === 0;
          if (dis) takebackBtn.setAttribute("disabled", "");
          else takebackBtn.removeAttribute("disabled");
          break;
        }
        case "game_result":
          ctx.log(`game_result: ${JSON.stringify(evt.payload)}`);
          resignBtn.setAttribute("disabled", "");
          toast(`Game over: ${evt.payload.result}`, { variant: "neutral" });
          break;
        case "system":
          ctx.log(`system: ${JSON.stringify(evt.payload)}`);
          break;
      }
    });

    // Render existing log lines on mount, then keep up via the custom event.
    eventLogEl.textContent = ctx.getLogSnapshot().join("\n");
    const onLog = (e) => append(eventLogEl, e.detail);
    window.addEventListener("sturddle:log", onLog);

    const onNewGame = async () => {
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

    newGameBtn.addEventListener("click", onNewGame);
    resignBtn.addEventListener("click", onResign);
    takebackBtn.addEventListener("click", onTakeback);

    return {
      unmount() {
        offEvent();
        view.unmount();
        window.removeEventListener("sturddle:log", onLog);
        window.removeEventListener("sturddle:settings-changed", onSettingsChanged);
        newGameBtn.removeEventListener("click", onNewGame);
        resignBtn.removeEventListener("click", onResign);
        takebackBtn.removeEventListener("click", onTakeback);
      },
    };
  },
};
