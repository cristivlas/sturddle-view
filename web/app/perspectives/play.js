// Play perspective: human vs engine.
// Layout: clock above board, board, clock below board, side-rail with move list.
// Engine debug + event log are gated behind a "Show debug" toggle.

import { mountBoard } from "../board.js";
import { toast } from "../dialogs.js";

function fmtClock(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return `${m}:${ss.toString().padStart(2, "0")}`;
}

function append(el, text, max = 200) {
  el.textContent = (el.textContent + text + "\n").split("\n").slice(-max).join("\n");
}

function renderMoveList(el, sanList) {
  // Two-column PGN-style: row per move-number with white | black.
  el.innerHTML = "";
  for (let i = 0; i < sanList.length; i += 2) {
    const row = document.createElement("div");
    row.className = "move-row";

    const num = document.createElement("span");
    num.className = "move-num";
    num.textContent = `${Math.floor(i / 2) + 1}.`;
    row.append(num);

    const white = document.createElement("span");
    white.className = "move-cell";
    white.textContent = sanList[i] ?? "";
    row.append(white);

    const black = document.createElement("span");
    black.className = "move-cell";
    black.textContent = sanList[i + 1] ?? "";
    row.append(black);

    el.append(row);
  }
  // Auto-scroll to bottom.
  el.scrollTop = el.scrollHeight;
}

export const playPerspective = {
  id: "play",
  label: "Play",

  async mount(root, ctx) {
    root.innerHTML = `
      <section id="play-perspective">
        <div class="play-board-area">
          <div class="clock-row clock-top" id="clock-top">
            <span class="clock-name">Engine</span>
            <span class="clock-time" id="clock-top-time">—</span>
          </div>

          <div id="board" aria-label="chess board"></div>

          <div class="clock-row clock-bottom" id="clock-bottom">
            <span class="clock-name">Human</span>
            <span class="clock-time" id="clock-bottom-time">—</span>
          </div>

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
        </div>

        <aside id="info-panel">
          <h2>Moves</h2>
          <div id="move-list" class="move-list"></div>

          <div class="debug-only" hidden>
            <h2>Engine info</h2>
            <pre id="engine-info"></pre>
            <h2>Event log</h2>
            <pre id="event-log"></pre>
          </div>
        </aside>
      </section>
    `;

    const clockTopName = root.querySelector("#clock-top .clock-name");
    const clockTopTime = root.querySelector("#clock-top-time");
    const clockTopRow = root.querySelector("#clock-top");
    const clockBottomName = root.querySelector("#clock-bottom .clock-name");
    const clockBottomTime = root.querySelector("#clock-bottom-time");
    const clockBottomRow = root.querySelector("#clock-bottom");
    const moveListEl = root.querySelector("#move-list");
    const engineInfoEl = root.querySelector("#engine-info");
    const eventLogEl = root.querySelector("#event-log");
    const debugRoot = root.querySelector(".debug-only");
    const showDebug = root.querySelector("#show-debug");
    const newGameBtn = root.querySelector("#new-game");
    const resignBtn = root.querySelector("#resign");
    const takebackBtn = root.querySelector("#takeback");

    // Settings cache; refreshed when /settings changes.
    let allowTakeback = true;

    async function refreshSettings() {
      try {
        const s = await ctx.api("GET", "/settings");
        allowTakeback = s.allow_takeback !== false;
        takebackBtn.hidden = !allowTakeback;
        if (!allowTakeback) takebackBtn.setAttribute("disabled", "");
      } catch {
        // ignore
      }
    }
    await refreshSettings();
    const onSettingsChanged = () => { refreshSettings(); };
    window.addEventListener("sturddle:settings-changed", onSettingsChanged);

    showDebug.addEventListener("change", () => {
      debugRoot.hidden = !showDebug.checked;
    });

    // Track which side is human so we know which clock-row is which.
    let humanWhite = true;
    let activeGameId = null;

    function setSideLabels() {
      // Bottom = human, top = engine. Always.
      clockBottomName.textContent = "Human";
      clockTopName.textContent = "Engine";
    }
    setSideLabels();

    function setClock({ white_time, black_time, turn, running }) {
      const humanTime = humanWhite ? white_time : black_time;
      const engineTime = humanWhite ? black_time : white_time;
      clockBottomTime.textContent = fmtClock(humanTime);
      clockTopTime.textContent = fmtClock(engineTime);

      const humanToMove = (turn === "white" && humanWhite) || (turn === "black" && !humanWhite);
      clockBottomRow.classList.toggle("active", running && humanToMove);
      clockTopRow.classList.toggle("active", running && !humanToMove);
    }

    const board = mountBoard({
      element: root.querySelector("#board"),
      onMove: async (uci) => {
        try {
          await ctx.api("POST", "/game/move", { uci });
        } catch (e) {
          toast(`Move rejected: ${e.message}`, { variant: "danger" });
        }
      },
    });

    const offEvent = ctx.events.on((evt) => {
      switch (evt.kind) {
        case "engine_info":
          append(engineInfoEl, JSON.stringify(evt.payload));
          break;
        case "board_update":
          if (typeof evt.payload.human_white === "boolean") {
            humanWhite = evt.payload.human_white;
            board.setSide(humanWhite ? "white" : "black");
          }
          board.setPosition(evt.payload.fen, evt.payload.last_move);
          renderMoveList(moveListEl, evt.payload.moves_san || []);
          board.enableInput(true);
          // Take-back is only meaningful once at least one ply has been played.
          {
            const dis =
              !allowTakeback || (evt.payload.moves_san?.length ?? 0) === 0;
            if (dis) takebackBtn.setAttribute("disabled", "");
            else takebackBtn.removeAttribute("disabled");
          }
          break;
        case "clock_tick":
          setClock(evt.payload);
          break;
        case "game_result":
          append(eventLogEl, `result: ${JSON.stringify(evt.payload)}`);
          board.enableInput(false);
          resignBtn.setAttribute("disabled", "");
          toast(`Game over: ${evt.payload.result}`, { variant: "neutral" });
          break;
        default:
          append(eventLogEl, `${evt.kind}: ${JSON.stringify(evt.payload)}`);
      }
    });

    const onNewGame = async () => {
      try {
        const r = await ctx.api("POST", "/game/new", {});
        activeGameId = r.game_id;
        humanWhite = !!r.human_white;
        board.setSide(humanWhite ? "white" : "black");
        resignBtn.removeAttribute("disabled");
        moveListEl.innerHTML = "";
        engineInfoEl.textContent = "";
      } catch (e) {
        toast(`New game failed: ${e.message}`, { variant: "danger" });
      }
    };

    const onResign = async () => {
      try {
        await ctx.api("POST", "/game/resign", {});
      } catch (e) {
        toast(`Resign failed: ${e.message}`, { variant: "danger" });
      }
    };

    const onTakeback = async () => {
      try {
        await ctx.api("POST", "/game/takeback", {});
      } catch (e) {
        toast(`Take-back failed: ${e.message}`, { variant: "danger" });
      }
    };

    newGameBtn.addEventListener("click", onNewGame);
    resignBtn.addEventListener("click", onResign);
    takebackBtn.addEventListener("click", onTakeback);

    return {
      unmount() {
        offEvent();
        window.removeEventListener("sturddle:settings-changed", onSettingsChanged);
        newGameBtn.removeEventListener("click", onNewGame);
        resignBtn.removeEventListener("click", onResign);
        takebackBtn.removeEventListener("click", onTakeback);
      },
    };
  },
};
