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
    sideContainer = null, // optional: separate host for the side rail
  } = opts;
  const showClocks = show.clocks !== false;
  const showMoves = show.moves !== false;
  const showEngineInfo = show.engineInfo !== false;

  // The board area always lives in `container`. The side rail goes into
  // `sideContainer` if provided, else inline below the board.
  container.innerHTML = `
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
  `;

  const sideHost = sideContainer ?? container;
  const sideHTML = `
    <aside class="game-view-side">
      ${showEngineInfo ? `
      <section class="game-view-engine">
        <div class="engine-summary">
          <span class="engine-depth">—</span>
          <span class="engine-score">—</span>
          <span class="engine-nps">—</span>
        </div>
        <div class="engine-pv"></div>
      </section>` : ""}

      ${showMoves ? `
      <section class="game-view-moves">
        <div class="move-list"></div>
      </section>` : ""}
    </aside>
  `;
  if (sideContainer) {
    sideContainer.innerHTML = sideHTML;
  } else {
    container.insertAdjacentHTML("beforeend", sideHTML);
  }

  const boardEl = container.querySelector(".board");
  const clockTopName = container.querySelector('[data-side="top"]');
  const clockTopTime = container.querySelector('[data-time="top"]');
  const clockBottomName = container.querySelector('[data-side="bottom"]');
  const clockBottomTime = container.querySelector('[data-time="bottom"]');
  const clockTopRow = container.querySelector(".clock-top");
  const clockBottomRow = container.querySelector(".clock-bottom");
  const moveListEl = sideHost.querySelector(".move-list");
  const engineDepth = sideHost.querySelector(".engine-depth");
  const engineScore = sideHost.querySelector(".engine-score");
  const engineNps = sideHost.querySelector(".engine-nps");
  const enginePv = sideHost.querySelector(".engine-pv");
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

  // cm-chessboard sizes its SVG off boardEl.clientWidth (squared), ignoring
  // height. We compute a square that fits the column width AND the viewport
  // height, then drive cm-chessboard's measurement.
  const boardCol = container.querySelector(".game-view-board") || container;

  function sumSiblingsBelow(node, gap) {
    // Only count siblings that are visually below `node` (greater top).
    // Grid layouts can place siblings beside, not below.
    const nodeRect = node.getBoundingClientRect();
    const nodeBottom = nodeRect.top + nodeRect.height;
    let total = 0;
    for (const sib of node.parentElement?.children ?? []) {
      if (sib === node) continue;
      if (sib.offsetParent === null) continue;
      const r = sib.getBoundingClientRect();
      if (r.top + 1 < nodeBottom) continue; // beside, not below
      total += r.height + gap;
    }
    return total;
  }

  function _readMainPaddingBottom() {
    const main = document.querySelector("main");
    if (!main) return 16;
    const v = parseFloat(getComputedStyle(main).paddingBottom);
    return Number.isFinite(v) ? v : 16;
  }

  function _recomputeNow() {
    if (!boardCol) return;
    boardEl.style.width = "0";
    const colRect = boardCol.getBoundingClientRect();

    let siblingsInCol = 0;
    for (const child of boardCol.children) {
      if (child === boardEl) continue;
      if (child.offsetParent === null) continue;
      siblingsInCol += child.getBoundingClientRect().height + 8;
    }

    let belowGameView = 0;
    let node = boardCol;
    while (node?.parentElement && node !== document.body) {
      belowGameView += sumSiblingsBelow(node, 12);
      const parent = node.parentElement;
      if (parent.id === "play-perspective" || parent.tagName === "MAIN") break;
      node = parent;
    }

    const bottomMargin = _readMainPaddingBottom() + 24;
    const availH = Math.max(
      0,
      window.innerHeight - colRect.top - siblingsInCol - belowGameView - bottomMargin
    );

    // Layout (wide viewports): [left-filler][board][rail], where the rail
    // and the left filler are the same width, so the board sits dead-center
    // horizontally. Rail width is viewport-driven (not board-driven) to
    // avoid a feedback loop with the board sizing below.
    const NARROW = 800;
    const MIN_BOARD = 320;
    const RAIL_MIN = 180;
    const RAIL_MAX = 320;
    const grid = boardCol.closest(".play-grid") || boardCol.closest("#play-perspective");
    const gridStyle = grid ? getComputedStyle(grid) : null;
    const gapW = gridStyle
      ? parseFloat(gridStyle.getPropertyValue("--grid-gap")) || 16
      : 16;
    const main = document.querySelector("main");
    const mainStyle = main ? getComputedStyle(main) : null;
    const sidePad = mainStyle
      ? parseFloat(mainStyle.paddingLeft) + parseFloat(mainStyle.paddingRight)
      : 32;

    let railW;
    let availW;
    if (window.innerWidth <= NARROW || !grid) {
      railW = 0;
      availW = Math.max(160, Math.floor(colRect.width));
    } else {
      const usable = window.innerWidth - sidePad;
      railW = Math.max(RAIL_MIN, Math.min(RAIL_MAX, Math.floor(usable * 0.18)));
      availW = Math.max(160, Math.floor(usable - 2 * railW - 2 * gapW));
    }

    const max = window.innerWidth <= NARROW
      ? availW
      // Honor the CSS minmax(320px, …) floor so the board doesn't go
      // below MIN_BOARD on awkward width-bound viewports (~800–900px).
      : Math.max(MIN_BOARD, Math.floor(Math.min(availW, availH)));

    boardEl.style.width = `${max}px`;
    const inner = boardEl.firstElementChild;
    if (inner) {
      inner.style.width = `${max}px`;
      inner.style.height = `${max}px`;
    }
    // Publish the computed board width so siblings (clocks, opening line,
    // controls bar) can clamp to the same width — and so the grid's
    // board column shrinks to that width, gluing the side rail next to it.
    boardCol.style.setProperty("--board-max-px", `${max}px`);
    if (grid) {
      grid.style.setProperty("--board-max-px", `${max}px`);
      // Only drive the column width on wide viewports; on narrow, the
      // grid collapses to a vertical flex layout (see CSS).
      if (window.innerWidth > NARROW) {
        grid.style.setProperty("--board-col-px", `${max}px`);
        grid.style.setProperty("--rail-w", `${railW}px`);
      } else {
        grid.style.removeProperty("--board-col-px");
        grid.style.removeProperty("--rail-w");
      }
      // Cap the side rail so the moves panel doesn't extend below the
      // board's bottom edge. Compute rail height = board bottom - rail top.
      const sideHost = grid.querySelector(".play-side-host");
      if (sideHost) {
        if (window.innerWidth > NARROW) {
          const boardRect = boardEl.getBoundingClientRect();
          const sideRect = sideHost.getBoundingClientRect();
          const target = Math.max(160, Math.floor(boardRect.bottom - sideRect.top));
          sideHost.style.setProperty("max-height", `${target}px`);
        } else {
          sideHost.style.removeProperty("max-height");
        }
      }
    }
    board.forceResize();
  }

  let recomputeRaf = 0;
  function recomputeBoardSize() {
    if (recomputeRaf) return;
    recomputeRaf = requestAnimationFrame(() => {
      recomputeRaf = 0;
      _recomputeNow();
      // Run again after the next paint so secondary measurements reflect
      // the new layout (e.g. controls/clocks settled into final positions).
      requestAnimationFrame(_recomputeNow);
    });
  }
  const ro = new ResizeObserver(recomputeBoardSize);
  ro.observe(boardCol);
  ro.observe(document.body);
  window.addEventListener("resize", recomputeBoardSize);
  window.addEventListener("sturddle:layout-changed", recomputeBoardSize);
  requestAnimationFrame(recomputeBoardSize);

  let humanWhite = true;
  let gameId = null;
  let engineName = "Engine";
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
    if (interactive) setNames({ bottom: "Human", top: engineName });
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
        if (evt.payload.engine_name) {
          engineName = evt.payload.engine_name;
          if (interactive) setNames({ top: engineName });
        }
        if (typeof evt.payload.human_white === "boolean") {
          humanWhite = evt.payload.human_white;
          board.setSide(humanWhite ? "white" : "black");
          if (interactive) setNames({ bottom: "Human", top: engineName });
        }
        board.setPosition(evt.payload.fen, evt.payload.last_move);
        board.clearArrows();
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
        if (evt.payload.pv_uci && evt.payload.pv_uci.length > 0) {
          const m = evt.payload.pv_uci[0];
          if (m && m.length >= 4) {
            board.setArrow(m.slice(0, 2), m.slice(2, 4));
          }
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
      try { ro.disconnect(); } catch {}
      window.removeEventListener("resize", recomputeBoardSize);
      window.removeEventListener("sturddle:layout-changed", recomputeBoardSize);
      if (recomputeRaf) cancelAnimationFrame(recomputeRaf);
    },
  };
}
