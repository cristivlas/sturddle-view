// Play perspective: human vs engine.
// Owns its own DOM: the board, the controls bar, and a fixed side rail.

import { mountBoard } from "../board.js";

export const playPerspective = {
  id: "play",
  label: "Play",

  async mount(root, ctx) {
    root.innerHTML = `
      <section id="play-perspective">
        <div class="play-board-area">
          <div id="board" aria-label="chess board"></div>
          <div id="board-controls">
            <wa-button id="new-game" size="small" variant="brand">New game</wa-button>
            <wa-select id="human-side" size="small" value="white">
              <wa-option value="white">White</wa-option>
              <wa-option value="black">Black</wa-option>
            </wa-select>
            <wa-input id="initial-seconds" size="small" type="number" min="1" value="300">
              <span slot="hint">time (s)</span>
            </wa-input>
            <wa-input id="increment-seconds" size="small" type="number" min="0" value="0">
              <span slot="hint">inc (s)</span>
            </wa-input>
            <wa-button id="resign" size="small" variant="danger" appearance="outlined">Resign</wa-button>
          </div>
        </div>
        <aside id="info-panel">
          <h2>Engine info</h2>
          <pre id="engine-info"></pre>
          <h2>Event log</h2>
          <pre id="event-log"></pre>
        </aside>
      </section>
    `;

    const engineInfo = root.querySelector("#engine-info");
    const eventLog = root.querySelector("#event-log");

    function append(el, text, max = 200) {
      el.textContent = (el.textContent + text + "\n").split("\n").slice(-max).join("\n");
    }

    const board = mountBoard({
      element: root.querySelector("#board"),
      onMove: async (uci) => {
        try {
          await ctx.api("POST", "/game/move", { uci });
        } catch (e) {
          append(eventLog, `move rejected: ${e.message}`);
        }
      },
    });

    const offEvent = ctx.events.on((evt) => {
      switch (evt.kind) {
        case "engine_info":
          append(engineInfo, JSON.stringify(evt.payload));
          break;
        case "board_update":
          board.setPosition(evt.payload.fen, evt.payload.last_move);
          board.enableInput(true);
          break;
        case "game_result":
          append(eventLog, `result: ${JSON.stringify(evt.payload)}`);
          board.enableInput(false);
          break;
        default:
          append(eventLog, `${evt.kind}: ${JSON.stringify(evt.payload)}`);
      }
    });

    const newGameBtn = root.querySelector("#new-game");
    const resignBtn = root.querySelector("#resign");

    const onNewGame = async () => {
      const side = root.querySelector("#human-side").value;
      const initial = parseFloat(root.querySelector("#initial-seconds").value);
      const inc = parseFloat(root.querySelector("#increment-seconds").value);
      board.setSide(side);
      try {
        const r = await ctx.api("POST", "/game/new", {
          human_white: side === "white",
          initial_seconds: initial,
          increment_seconds: inc,
        });
        append(eventLog, `new game: ${r.game_id}`);
      } catch (e) {
        append(eventLog, `new-game failed: ${e.message}`);
      }
    };

    const onResign = async () => {
      try {
        await ctx.api("POST", "/game/resign", {});
      } catch (e) {
        append(eventLog, `resign failed: ${e.message}`);
      }
    };

    newGameBtn.addEventListener("click", onNewGame);
    resignBtn.addEventListener("click", onResign);

    return {
      unmount() {
        offEvent();
        newGameBtn.removeEventListener("click", onNewGame);
        resignBtn.removeEventListener("click", onResign);
      },
    };
  },
};
