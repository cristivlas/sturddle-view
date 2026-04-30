// GameView: reusable visual representation of one game.
//
// Composed of: board, two clocks (top + bottom), move list, engine info.
// Each aspect can be shown or hidden. Drives itself from event-bus events
// scoped to a `gameId` (the caller is responsible for routing).
//
// Used by:
//   - Play perspective (interactive, full-size)
//   - Observe perspective (read-only, inside a WinBox window)
//
// API:
//   const view = mountGameView(container, {
//     events,              // ctx.events
//     onMove(uci),         // optional; only called when interactive
//     show: { clocks, moves, engineInfo },  // each defaults to true
//     interactive: false,  // if true, board accepts user moves
//   })
//   view.setGameId(id)         // start tracking events for this game
//   view.setHumanWhite(bool)   // when interactive, sets board orientation
//   view.applyEvent(evt)       // alternative to subscribing through events bus
//   view.unmount()

import { mountBoard } from "./board.js";

function fmtClock(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return `${m}:${ss.toString().padStart(2, "0")}`;
}

function renderMoveList(el, sanList) {
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
  el.scrollTop = el.scrollHeight;
}

function fmtScore(score) {
  if (!score) return "";
  if ("mate" in score) return `#${score.mate}`;
  if ("cp" in score) {
    const cp = score.cp;
    return `${cp >= 0 ? "+" : ""}${(cp / 100).toFixed(2)}`;
  }
  return "";
}

export function mountGameView(container, opts = {}) {
  const {
    events,
    onMove,
    show = {},
    interactive = false,
  } = opts;
  const showClocks = show.clocks !== false;
  const showMoves = show.moves !== false;
  const showEngineInfo = show.engineInfo !== false;

  container.innerHTML = `
    <div class="game-view">
      <div class="game-view-board">
        ${showClocks ? `
        <div class="clock-row clock-top">
          <span class="clock-name" data-side="top">—</span>
          <span class="clock-time" data-time="top">—</span>
        </div>` : ""}

        <div class="board" aria-label="chess board"></div>

        <div class="game-view-meta">
          <div class="opening-line" hidden>
            <span class="opening-eco"></span>
            <span class="opening-name"></span>
          </div>
          <div class="tablebase-line" hidden>
            <span class="tb-label">TB</span>
            <span class="tb-result"></span>
          </div>
        </div>

        ${showClocks ? `
        <div class="clock-row clock-bottom">
          <span class="clock-name" data-side="bottom">—</span>
          <span class="clock-time" data-time="bottom">—</span>
        </div>` : ""}
      </div>

      <aside class="game-view-side">
        ${showMoves ? `
        <section class="game-view-moves">
          <h2>Moves</h2>
          <div class="move-list"></div>
        </section>` : ""}

        ${showEngineInfo ? `
        <section class="game-view-engine">
          <h2>Engine</h2>
          <div class="engine-summary">
            <span class="engine-depth">—</span>
            <span class="engine-score">—</span>
            <span class="engine-nps">—</span>
          </div>
          <div class="engine-pv"></div>
        </section>` : ""}
      </aside>
    </div>
  `;

  const boardEl = container.querySelector(".board");
  const clockTopName = container.querySelector('[data-side="top"]');
  const clockTopTime = container.querySelector('[data-time="top"]');
  const clockBottomName = container.querySelector('[data-side="bottom"]');
  const clockBottomTime = container.querySelector('[data-time="bottom"]');
  const clockTopRow = container.querySelector(".clock-top");
  const clockBottomRow = container.querySelector(".clock-bottom");
  const moveListEl = container.querySelector(".move-list");
  const engineDepth = container.querySelector(".engine-depth");
  const engineScore = container.querySelector(".engine-score");
  const engineNps = container.querySelector(".engine-nps");
  const enginePv = container.querySelector(".engine-pv");
  const openingLine = container.querySelector(".opening-line");
  const openingEco = container.querySelector(".opening-eco");
  const openingName = container.querySelector(".opening-name");
  const tbLine = container.querySelector(".tablebase-line");
  const tbResult = container.querySelector(".tb-result");

  function setOpening(opening) {
    if (!openingLine) return;
    if (!opening || (!opening.eco && !opening.name)) {
      openingLine.hidden = true;
      return;
    }
    openingEco.textContent = opening.eco ?? "";
    openingName.textContent = opening.name ?? "";
    openingLine.hidden = false;
  }

  function setTablebase(tb) {
    if (!tbLine) return;
    if (!tb || tb.wdl === undefined || tb.wdl === null) {
      tbLine.hidden = true;
      return;
    }
    const wdl = ({ 2: "Win", 1: "Cursed win", 0: "Draw", "-1": "Blessed loss", "-2": "Loss" })[tb.wdl] ?? "—";
    let s = wdl;
    if (Number.isFinite(tb.dtz)) s += ` · DTZ ${tb.dtz}`;
    if (Number.isFinite(tb.dtm)) s += ` · DTM ${tb.dtm}`;
    if (tb.best) s += ` · ${tb.best}`;
    tbResult.textContent = s;
    tbLine.hidden = false;
  }

  const board = mountBoard({
    element: boardEl,
    onMove: (uci) => {
      if (interactive) onMove?.(uci);
    },
  });

  let humanWhite = true;
  let gameId = null;
  let names = { top: "—", bottom: "—" };

  function setNames({ top, bottom } = {}) {
    if (top !== undefined) {
      names.top = top;
      if (clockTopName) clockTopName.textContent = top;
    }
    if (bottom !== undefined) {
      names.bottom = bottom;
      if (clockBottomName) clockBottomName.textContent = bottom;
    }
  }

  function setHumanWhite(value) {
    humanWhite = !!value;
    board.setSide(humanWhite ? "white" : "black");
    // In interactive (Play) mode, bottom = human, top = engine.
    if (interactive) setNames({ bottom: "Human", top: "Engine" });
  }
  setHumanWhite(humanWhite);

  function setClock({ white_time, black_time, turn, running }) {
    if (!showClocks) return;
    // bottom = humanWhite ? white : black; in observe, bottom = white, top = black
    const bottomIsWhite = interactive ? humanWhite : true;
    const bottomTime = bottomIsWhite ? white_time : black_time;
    const topTime = bottomIsWhite ? black_time : white_time;
    if (clockBottomTime) clockBottomTime.textContent = fmtClock(bottomTime);
    if (clockTopTime) clockTopTime.textContent = fmtClock(topTime);

    const bottomToMove =
      (turn === "white" && bottomIsWhite) || (turn === "black" && !bottomIsWhite);
    clockBottomRow?.classList.toggle("active", running && bottomToMove);
    clockTopRow?.classList.toggle("active", running && !bottomToMove);
  }

  function applyEvent(evt) {
    if (!evt) return;
    if (gameId !== null && evt.game_id && evt.game_id !== gameId) return;
    switch (evt.kind) {
      case "board_update":
        if (typeof evt.payload.human_white === "boolean") {
          humanWhite = evt.payload.human_white;
          board.setSide(humanWhite ? "white" : "black");
        }
        board.setPosition(evt.payload.fen, evt.payload.last_move);
        if (showMoves && moveListEl) {
          renderMoveList(moveListEl, evt.payload.moves_san || []);
        }
        setOpening(evt.payload.opening);
        setTablebase(evt.payload.tablebase);
        if (interactive) board.enableInput(true);
        break;
      case "clock_tick":
        setClock(evt.payload);
        break;
      case "engine_info":
        if (!showEngineInfo) break;
        if (engineDepth && evt.payload.depth != null) {
          engineDepth.textContent = `d${evt.payload.depth}`;
        }
        if (engineScore && evt.payload.score) {
          engineScore.textContent = fmtScore(evt.payload.score);
        }
        if (engineNps && evt.payload.nps != null) {
          const k = evt.payload.nps / 1000;
          engineNps.textContent = `${k >= 100 ? Math.round(k) : k.toFixed(1)} kn/s`;
        }
        if (enginePv && evt.payload.pv && evt.payload.pv.length > 0) {
          enginePv.textContent = evt.payload.pv[0];
        }
        break;
      case "game_result":
        if (interactive) board.enableInput(false);
        break;
    }
  }

  let off = null;
  if (events) {
    off = events.on(applyEvent);
  }

  return {
    setGameId(id) {
      gameId = id;
    },
    setHumanWhite,
    setNames,
    applyEvent,
    setEnabled(enabled) {
      board.enableInput(interactive && enabled);
    },
    reset() {
      // Reset visible game state for a fresh game; the next board_update
      // from the server will set the new starting position.
      board.setPosition("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", null);
      if (moveListEl) moveListEl.innerHTML = "";
      if (engineDepth) engineDepth.textContent = "—";
      if (engineScore) engineScore.textContent = "—";
      if (engineNps) engineNps.textContent = "—";
      if (enginePv) enginePv.textContent = "";
      setOpening(null);
      setTablebase(null);
    },
    unmount() {
      off?.();
    },
  };
}
