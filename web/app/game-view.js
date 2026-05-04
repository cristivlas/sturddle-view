// GameView: board + clocks + move list + engine info, scoped to a gameId.
// Used by Play (interactive, full-size) and Observe (read-only, in a WinBox).

import { mountBoard } from "./board.js";
import { toast } from "./dialogs.js";

function fmtClock(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const t = Math.max(0, seconds);
  // Sub-10s: show tenths so bullet/sub-second-increment games are readable.
  if (t < 10) return t.toFixed(1);
  const s = Math.floor(t);
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return `${m}:${ss.toString().padStart(2, "0")}`;
}

function renderMoveList(el, sanList, currentIdx = null, onMoveClick = null) {
  // currentIdx: index of the highlighted ply, or null for "last" (play mode).
  // onMoveClick(plyIndex): when provided, each move cell becomes clickable
  // and invokes the callback with its 0-based ply index. Used in view mode
  // to jump the cursor to the clicked move.
  el.innerHTML = "";
  const lastIdx = sanList.length - 1;
  const highlightIdx = currentIdx == null ? lastIdx : currentIdx;
  let highlightedRow = null;
  const makeCell = (san, plyIdx) => {
    const cell = document.createElement("span");
    cell.className = "move-cell";
    if (!san) return cell;
    cell.textContent = san;
    if (onMoveClick) {
      cell.classList.add("clickable");
      cell.addEventListener("click", () => onMoveClick(plyIdx));
    }
    return cell;
  };
  for (let i = 0; i < sanList.length; i += 2) {
    const row = document.createElement("div");
    row.className = "move-row";

    const num = document.createElement("span");
    num.className = "move-num";
    num.textContent = `${Math.floor(i / 2) + 1}.`;
    row.append(num);

    const white = makeCell(sanList[i], i);
    if (i === highlightIdx) {
      white.classList.add("is-current");
      highlightedRow = row;
    }
    row.append(white);

    const black = makeCell(sanList[i + 1], i + 1);
    if (i + 1 === highlightIdx) {
      black.classList.add("is-current");
      highlightedRow = row;
    }
    row.append(black);

    el.append(row);
  }
  // Auto-scroll: in play mode (no explicit cursor) keep the latest move
  // visible; in view mode keep the cursor visible as the user scrubs.
  if (currentIdx == null) {
    el.scrollTop = el.scrollHeight;
  } else if (highlightedRow) {
    highlightedRow.scrollIntoView({ block: "nearest" });
  }
}

function fmtCount(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
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
    onMoveJump = null, // view-mode click on a move; (plyIndex) => void
    show = {},
    interactive = false,
    sideContainer = null, // optional: separate host for the side rail
    boardStyle = null,    // preset id from settings; null = library default
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
        <div class="fen-line">
          <button type="button" class="fen-copy" aria-label="Copy FEN">
            <wa-icon name="copy"></wa-icon>
          </button>
          <span class="fen-text" title="Click to copy"></span>
        </div>
        <div class="opening-line is-empty">
          <span class="opening-eco"></span>
          <span class="opening-name"></span>
        </div>
        <div class="tablebase-line is-empty">
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
      <section class="game-view-engine is-empty">
        <div class="engine-summary">
          <div class="engine-stat" data-label="Score"><span class="engine-score"></span></div>
          <div class="engine-stat" data-label="Depth"><span class="engine-depth"></span></div>
          <div class="engine-stat" data-label="Nodes"><span class="engine-nodes"></span></div>
          <div class="engine-stat" data-label="Nps"><span class="engine-nps"></span></div>
          <div class="engine-stat" data-label="Hash"><span class="engine-hashfull"></span></div>
          <div class="engine-stat" data-label="TB"><span class="engine-tbhits"></span></div>
        </div>
        <div class="engine-pv" title=""></div>
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
  const engineNodes = sideHost.querySelector(".engine-nodes");
  const engineNps = sideHost.querySelector(".engine-nps");
  const engineTbhits = sideHost.querySelector(".engine-tbhits");
  const engineHashfull = sideHost.querySelector(".engine-hashfull");
  const engineSection = sideHost.querySelector(".game-view-engine");
  const enginePv = sideHost.querySelector(".engine-pv");
  const openingLine = container.querySelector(".opening-line");
  const openingEco = container.querySelector(".opening-eco");
  const openingName = container.querySelector(".opening-name");
  const tbLine = container.querySelector(".tablebase-line");
  const tbResult = container.querySelector(".tb-result");
  const fenText = container.querySelector(".fen-text");
  const fenCopyBtn = container.querySelector(".fen-copy");

  let currentFen = "";
  function setFen(fen) {
    currentFen = fen || "";
    if (fenText) fenText.textContent = currentFen;
  }
  async function copyFen() {
    if (!currentFen) return;
    // Prefer the async Clipboard API (works on https + localhost). Fall
    // back to the legacy execCommand path for plain-http hosts where the
    // async API is blocked.
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(currentFen);
      } else {
        const ta = document.createElement("textarea");
        ta.value = currentFen;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand("copy");
        ta.remove();
        if (!ok) throw new Error("execCommand failed");
      }
      toast("FEN copied", { variant: "success", duration: 1500 });
    } catch {
      toast("Could not copy FEN", { variant: "danger" });
    }
  }
  fenCopyBtn?.addEventListener("click", copyFen);
  fenText?.addEventListener("click", copyFen);

  function setOpening(opening) {
    if (!openingLine) return;
    if (!opening || (!opening.eco && !opening.name)) {
      openingLine.classList.add("is-empty");
      return;
    }
    openingEco.textContent = opening.eco ?? "";
    openingName.textContent = opening.name ?? "";
    openingLine.classList.remove("is-empty");
  }

  function setTablebase(tb) {
    if (!tbLine) return;
    if (!tb || tb.wdl === undefined || tb.wdl === null) {
      tbLine.classList.add("is-empty");
      return;
    }
    const wdl = ({ 2: "Win", 1: "Cursed win", 0: "Draw", "-1": "Blessed loss", "-2": "Loss" })[tb.wdl] ?? "—";
    let s = wdl;
    if (Number.isFinite(tb.dtz)) s += ` · DTZ ${tb.dtz}`;
    if (Number.isFinite(tb.dtm)) s += ` · DTM ${tb.dtm}`;
    if (tb.best) s += ` · ${tb.best}`;
    tbResult.textContent = s;
    tbLine.classList.remove("is-empty");
  }

  const board = mountBoard({
    element: boardEl,
    styleId: boardStyle,
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
      // Align the side rail's top with the board's top (the grid would
       // otherwise place it next to the top clock row), and cap its height
       // at the board's height so the moves panel stays within the board.
      const sideHost = grid.querySelector(".play-side-host");
      if (sideHost) {
        if (window.innerWidth > NARROW) {
          const boardRect = boardEl.getBoundingClientRect();
          // Reset margin before measuring so the offset reflects the
          // grid-natural top, not last frame's adjustment.
          sideHost.style.marginTop = "0px";
          const sideRect = sideHost.getBoundingClientRect();
          const offset = Math.max(0, Math.floor(boardRect.top - sideRect.top));
          sideHost.style.marginTop = `${offset}px`;
          const target = Math.max(160, Math.floor(boardRect.height));
          sideHost.style.setProperty("max-height", `${target}px`);
        } else {
          sideHost.style.removeProperty("max-height");
          sideHost.style.removeProperty("margin-top");
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

  function _truncName(s, max = 24) {
    if (!s) return s;
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }
  function setNames({ top, bottom } = {}) {
    if (top !== undefined) {
      names.top = top;
      if (clockTopName) clockTopName.textContent = _truncName(top);
    }
    if (bottom !== undefined) {
      names.bottom = bottom;
      if (clockBottomName) clockBottomName.textContent = _truncName(bottom);
    }
  }

  function setHumanWhite(value) {
    humanWhite = !!value;
    board.setSide(humanWhite ? "white" : "black");
    // In interactive (Play) mode, bottom = human, top = engine.
    if (interactive) setNames({ bottom: "Human", top: engineName });
  }
  setHumanWhite(humanWhite);

  function setClock({ white_time, black_time, turn, running, viewing }) {
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
    // View mode: clocks are historical snapshots, frozen — visually mute
    // both rows (no "active" highlight, dimmed via .clock-disabled).
    clockBottomRow?.classList.toggle("clock-disabled", !!viewing);
    clockTopRow?.classList.toggle("clock-disabled", !!viewing);
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
        // View mode: surface the PGN's player names instead of Human/engine.
        if (interactive && evt.payload.view) {
          const w = evt.payload.view.white_name || "White";
          const b = evt.payload.view.black_name || "Black";
          // Bottom is white when not flipped (humanWhite acts as the orient
          // toggle even in view mode).
          if (humanWhite) setNames({ bottom: w, top: b });
          else setNames({ bottom: b, top: w });
        }
        board.setPosition(evt.payload.fen, evt.payload.last_move);
        setFen(evt.payload.fen);
        board.clearArrows();
        if (showMoves && moveListEl) {
          // View mode highlights the cursor's ply (cursor-1 = last played
          // move; cursor=0 means initial position → no highlight) and lets
          // the user jump by clicking a move in the list.
          let currentIdx = null;
          let clickHandler = null;
          if (evt.payload.view) {
            currentIdx = (evt.payload.view.cursor ?? 0) - 1;
            clickHandler = onMoveJump;
          }
          renderMoveList(
            moveListEl, evt.payload.moves_san || [], currentIdx, clickHandler,
          );
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
        engineSection?.classList.remove("is-empty");
        if (engineDepth && evt.payload.depth != null) {
          engineDepth.textContent = evt.payload.depth;
        }
        if (engineScore && evt.payload.score) {
          engineScore.textContent = fmtScore(evt.payload.score);
        }
        if (engineNodes && evt.payload.nodes != null) {
          engineNodes.textContent = fmtCount(evt.payload.nodes);
        }
        if (engineNps && evt.payload.nps != null) {
          engineNps.textContent = fmtCount(evt.payload.nps);
        }
        if (engineTbhits) {
          engineTbhits.textContent = evt.payload.tbhits ? fmtCount(evt.payload.tbhits) : "";
        }
        if (engineHashfull && evt.payload.hashfull != null) {
          engineHashfull.textContent = `${(evt.payload.hashfull / 10).toFixed(0)}%`;
        }
        if (enginePv && evt.payload.pv && evt.payload.pv.length > 0) {
          const full = evt.payload.pv.join(" ");
          enginePv.textContent = full;
          enginePv.setAttribute("title", full);
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
      if (engineDepth) engineDepth.textContent = "";
      if (engineScore) engineScore.textContent = "";
      if (engineNodes) engineNodes.textContent = "";
      if (engineNps) engineNps.textContent = "";
      if (engineTbhits) engineTbhits.textContent = "";
      if (engineHashfull) engineHashfull.textContent = "";
      if (enginePv) enginePv.textContent = "";
      engineSection?.classList.add("is-empty");
      setOpening(null);
      setTablebase(null);
      setFen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
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
