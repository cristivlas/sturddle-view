// GameView: board + clocks + move list + engine info, scoped to a gameId.
// Used by Play (interactive, full-size) and Observe (read-only, in a WinBox).

import { mountBoard } from "./board.js";
import { toast } from "./dialogs.js";
import { isMobileLayout } from "./play-debug-windows.js";

const INITIAL_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

// Clock-row name cap: largest value that fits beside the clock on the
// narrowest desktop board (~400px at the 800px viewport breakpoint).
const MAX_CLOCK_NAME_DESKTOP = 32;
const MAX_CLOCK_NAME_MOBILE = 24;

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

function renderMoveList(
  el, sanList, currentIdx = null, onMoveClick = null,
  forkChildCounts = null, onForkClick = null,
) {
  // currentIdx: index of the highlighted ply, or null for "last" (play mode).
  // onMoveClick(plyIndex): when provided, each move cell becomes clickable
  // and invokes the callback with its 0-based ply index. Used in view mode
  // to jump the cursor to the clicked move.
  // forkChildCounts (optional): Map<plyIndex, count> of plies that have
  // forked children in the x-game tree. Plies in the map get an inline
  // fork glyph + count.
  // onForkClick(plyIndex): when provided, the glyph itself is clickable
  // and invokes this callback (in addition to the move-cell click that
  // also navigates). Used to re-show a dismissed parent->child banner.
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
    const childCount = forkChildCounts ? forkChildCounts.get(plyIdx) : 0;
    if (childCount > 0) {
      cell.classList.add("has-fork");
      const glyph = document.createElement("wa-icon");
      glyph.setAttribute("name", "code-fork");
      glyph.className = "fork-glyph";
      glyph.title = childCount === 1
        ? "1 variation from this position"
        : `${childCount} variations from this position`;
      if (onForkClick) {
        glyph.classList.add("clickable");
        glyph.addEventListener("click", (e) => {
          e.stopPropagation();
          onForkClick(plyIdx);
        });
      }
      cell.append(" ", glyph);
      if (childCount > 1) {
        const cnt = document.createElement("span");
        cnt.className = "fork-count";
        cnt.textContent = String(childCount);
        cell.append(cnt);
      }
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
    forkChildCountsFn = null, // () => Map<plyIdx, count> for fork glyphs
    onForkClick = null, // (plyIdx) => void when glyph itself is clicked
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
          <span class="tb-indent"></span>
          <span class="hm-clock"></span>
          <span class="tb-info"></span>
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
  const tbInfo = container.querySelector(".tb-info");
  const hmClock = container.querySelector(".hm-clock");
  const fenText = container.querySelector(".fen-text");
  const fenCopyBtn = container.querySelector(".fen-copy");

  let currentFen = INITIAL_FEN;
  if (fenText) fenText.textContent = INITIAL_FEN;
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
    const hm = tb && Number.isFinite(tb.halfmove_clock) ? tb.halfmove_clock : null;
    const hasTb = tb && tb.wdl !== undefined && tb.wdl !== null;
    if (!hasTb && hm === null) {
      tbLine.classList.add("is-empty");
      return;
    }
    if (tbInfo) {
      if (hasTb) {
        const wdl = ({ 2: "Win", 1: "Cursed win", 0: "Draw", "-1": "Blessed loss", "-2": "Loss" })[tb.wdl] ?? "--";
        let s = hm !== null ? ` · ${wdl}` : wdl;
        if (Number.isFinite(tb.dtz)) s += ` · DTZ ${tb.dtz}`;
        if (Number.isFinite(tb.dtm)) s += ` · DTM ${tb.dtm}`;
        if (tb.best) s += ` · ${tb.best}`;
        tbInfo.textContent = s;
      } else {
        tbInfo.textContent = "";
      }
    }
    if (hmClock) {
      hmClock.textContent = hm !== null ? `50-move rule: ${hm}/100` : "";
      hmClock.classList.toggle("hm-clock-warn", hm !== null && hm >= 40);
    }
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
    // Rail/board minimums are derived from root font-size so they honor
    // the user's browser font-size preference. NARROW stays raw CSS-px:
    // it must match the CSS `@media (max-width: 640px)` mobile breakpoint
    // (which is viewport-driven, not font-size-driven) -- otherwise JS
    // flips to mobile mode while CSS stays desktop, stranding the rail
    // in the left filler column at large font sizes.
    const rootFs = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    const rem = (n) => Math.round(n * rootFs);
    const NARROW = 640;
    const MIN_BOARD = rem(20);   // 320px @ default fs
    const RAIL_MIN = rem(11.25); // 180px @ default fs
    const RAIL_MAX = rem(20);    // 320px @ default fs
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
    // leftRailW / --left-rail-w name the dock-side column, not a screen side:
    // when ribbon_side="right", the mirror swaps grid columns so this width
    // applies to the right rail. Shrink-when-empty still tracks the dock.
    let leftRailW;
    let availW;
    // When the left dock is empty on desktop, shrink the left rail so the
    // board + right rail shift left as one block instead of being framed
    // by a wide empty band. Proportional to railW so it scales with width.
    const LEFT_RAIL_EMPTY_RATIO = window.__leftRailEmptyRatio ?? 0.4;
    if (window.innerWidth <= NARROW || !grid) {
      railW = 0;
      leftRailW = 0;
      availW = Math.max(rem(10), Math.floor(colRect.width));
    } else {
      const usable = window.innerWidth - sidePad;
      railW = Math.max(RAIL_MIN, Math.min(RAIL_MAX, Math.floor(usable * 0.18)));
      const leftEmpty =
        document.querySelector(".play-dock-left")?.classList.contains("dock-empty") !== false
        && document.querySelector(".play-comments-host")?.classList.contains("dock-empty") !== false;
      leftRailW = leftEmpty ? Math.floor(railW * LEFT_RAIL_EMPTY_RATIO) : railW;
      availW = Math.max(rem(10), Math.floor(usable - railW - leftRailW - 2 * gapW));
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
        grid.style.setProperty("--left-rail-w", `${leftRailW}px`);
      } else {
        grid.style.removeProperty("--board-col-px");
        grid.style.removeProperty("--left-rail-w");
      }
      // Align the side rail's top with the board's top (the grid would
       // otherwise place it next to the top clock row), and cap its height
       // at the board's height so the moves panel stays within the board.
      const sideHost = grid.querySelector(".play-side-host");
      if (sideHost) {
        if (window.innerWidth > NARROW) {
          const boardRect = boardEl.getBoundingClientRect();
          const ribbonRight = document.body.dataset.ribbonSide === "right";
          const top = Math.ceil(boardRect.top);
          // On wide viewports, cap the side rail at its natural width so it
          // doesn't stretch all the way to the edge -- combined with the
          // shrunken opposite rail (when dock is empty), this keeps the
          // picture centered instead of framed by a wide empty band.
          // Kept as a raw CSS-px threshold (not rem-derived): the
          // below-WIDE branch lets the rail expand to fill `avail`, and
          // scaling WIDE up with font-size pushes viewports into that
          // expanding branch where the rail visibly slides as the text
          // grows. The wide-viewport feel is a viewport property, not a
          // font-size one.
          const WIDE = 1500;
          let left;
          let avail;
          if (ribbonRight) {
            avail = Math.max(0, Math.ceil(boardRect.left) - gapW - rem(1));
            const width = window.innerWidth >= WIDE ? Math.min(railW, avail) : avail;
            left = Math.max(rem(1), Math.ceil(boardRect.left) - gapW - width);
            const height = Math.max(rem(10), Math.floor(boardRect.height));
            sideHost.style.left = `${left}px`;
            sideHost.style.top = `${top}px`;
            sideHost.style.width = `${width}px`;
            sideHost.style.setProperty("max-height", `${height}px`);
          } else {
            left = Math.ceil(boardRect.right) + gapW;
            avail = Math.max(0, window.innerWidth - left - rem(1));
            const width = window.innerWidth >= WIDE ? Math.min(railW, avail) : avail;
            const height = Math.max(rem(10), Math.floor(boardRect.height));
            sideHost.style.left = `${left}px`;
            sideHost.style.top = `${top}px`;
            sideHost.style.width = `${width}px`;
            sideHost.style.setProperty("max-height", `${height}px`);
          }
          sideHost.style.removeProperty("margin-top");
        } else {
          sideHost.style.removeProperty("max-height");
          sideHost.style.removeProperty("margin-top");
          sideHost.style.removeProperty("left");
          sideHost.style.removeProperty("top");
          sideHost.style.removeProperty("width");
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

  function onVisibilityChange() {
    if (!document.hidden) board.cancelAnimations();
  }
  function onWindowFocus() {
    board.cancelAnimations();
  }
  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("focus", onWindowFocus);

  let humanWhite = true;
  let gameId = null;
  let engineName = "Engine";
  let names = { top: "—", bottom: "—" };
  let lastTurn = "white";
  let lastClockRunning = false;
  let viewing = false;
  let editing = false;
  // First board_update after (re)mount: snap pieces to position instead
  // of animating from startpos, and resolve the `ready` Promise so the
  // PerspectiveRouter can reveal the perspective. Otherwise the user
  // sees clocks/side-rail render before the board, and pieces animate
  // from cm-chessboard's default startpos to the real FEN.
  let firstBoardUpdate = true;
  let resolveReady;
  const ready = new Promise((r) => { resolveReady = r; });
  // Edit-mode side-to-move ("w"|"b"). Authoritative while editing; play.js
  // mirrors it for its ribbon UI but defers to setEditSide for writes.
  let editStm = "w";
  // Analysis mode: streams PV from a dedicated engine even while
  // viewing. PV row should hide when "view-only" (viewing && !analyzing)
  // and show otherwise -- play mode has PV from the play engine,
  // analysis mode has PV from the analysis engine.
  let analyzing = false;
  // Cached PGN names so flipping the board in view mode can re-swap
  // top/bottom without waiting for a fresh board_update.
  let viewWhiteName = null;
  let viewBlackName = null;

  function _truncName(s) {
    if (!s) return s;
    const max = isMobileLayout() ? MAX_CLOCK_NAME_MOBILE : MAX_CLOCK_NAME_DESKTOP;
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }
  // PV row hides only in pure view mode (navigating an imported game
  // with no engine running). Play mode and analysis mode both produce
  // a meaningful PV.
  function syncPvVisibility() {
    const hidePv = viewing && !analyzing;
    if (enginePv) enginePv.classList.toggle("hidden", hidePv);
    if (engineSection) engineSection.classList.toggle("no-pv", hidePv);
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
    // In interactive (Play) mode, bottom = human, top = engine. In view
    // mode, re-swap cached PGN names to match the new orientation.
    if (interactive && !viewing) {
      setNames({ bottom: "Human", top: engineName });
    } else if (viewing && viewWhiteName !== null) {
      if (humanWhite) setNames({ bottom: viewWhiteName, top: viewBlackName });
      else setNames({ bottom: viewBlackName, top: viewWhiteName });
    }
    // Re-apply clock colors and active state: clock_tick won't fire until
    // the next server event, so do it eagerly here for both edit and view.
    if (showClocks) {
      _applyClockColors();
      if (editing) {
        _applyClockActive(editStm === "b" ? "black" : "white", true);
      } else {
        _applyClockActive(lastTurn, lastClockRunning);
      }
    }
  }
  setHumanWhite(humanWhite);

  function _bottomIsWhite() {
    // In Observe (non-interactive) the bottom row is always white. In Play
    // the bottom is the human's side.
    return interactive ? humanWhite : true;
  }

  function _applyClockActive(turn, active) {
    const bottomIsWhite = _bottomIsWhite();
    const bottomToMove =
      (turn === "white" && bottomIsWhite) || (turn === "black" && !bottomIsWhite);
    clockBottomRow?.classList.toggle("active", active && bottomToMove);
    clockTopRow?.classList.toggle("active", active && !bottomToMove);
  }

  function _applyClockColors() {
    if (!showClocks) return;
    const bottomIsWhite = _bottomIsWhite();
    if (clockBottomRow) clockBottomRow.dataset.color = bottomIsWhite ? "white" : "black";
    if (clockTopRow) clockTopRow.dataset.color = bottomIsWhite ? "black" : "white";
  }

  function setClock({ white_time, black_time, turn, running, viewing }) {
    if (!showClocks) return;
    lastTurn = turn || "white";
    lastClockRunning = running || !!viewing;
    const bottomIsWhite = _bottomIsWhite();
    const bottomTime = bottomIsWhite ? white_time : black_time;
    const topTime = bottomIsWhite ? black_time : white_time;
    if (clockBottomTime) clockBottomTime.textContent = fmtClock(bottomTime);
    if (clockTopTime) clockTopTime.textContent = fmtClock(topTime);
    _applyClockColors();
    _applyClockActive(lastTurn, lastClockRunning);
  }

  function applyEvent(evt) {
    if (!evt) return;
    if (gameId !== null && evt.game_id && evt.game_id !== gameId) return;
    switch (evt.kind) {
      case "board_update":
        viewing = !!evt.payload.view;
        if (typeof evt.payload.editing === "boolean") {
          const wasEditing = editing;
          editing = evt.payload.editing;
          if (editing && !wasEditing) board.clearArrows();
        }
        if (typeof evt.payload.analyzing === "boolean") {
          analyzing = evt.payload.analyzing;
        }
        syncPvVisibility();
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
          viewWhiteName = w;
          viewBlackName = b;
          // Bottom is white when not flipped (humanWhite acts as the orient
          // toggle even in view mode).
          if (humanWhite) setNames({ bottom: w, top: b });
          else setNames({ bottom: b, top: w });
        }
        // Skip setPosition during edit so the user's in-progress board
        // edits aren't clobbered by server state. Exception: cold mount
        // mid-edit (firstBoardUpdate) -- there are no in-progress edits
        // yet, and the board is at the cm-chessboard default startpos;
        // we must seed it from the server's authoritative FEN.
        if (!editing || firstBoardUpdate) {
          board.setPosition(evt.payload.fen, evt.payload.last_move, !firstBoardUpdate);
        }
        if (firstBoardUpdate) {
          firstBoardUpdate = false;
          resolveReady();
        }
        setFen(evt.payload.fen);
        if (!editing) board.clearArrows();
        if (showMoves && moveListEl) {
          // View mode highlights the cursor's ply (cursor-1 = last played
          // move; cursor=0 means initial position → no highlight) and lets
          // the user jump by clicking a move in the list.
          let currentIdx = null;
          let clickHandler = null;
          if (evt.payload.view && !editing) {
            currentIdx = (evt.payload.view.cursor ?? 0) - 1;
            clickHandler = onMoveJump;
          }
          // x-game fork glyphs: only meaningful in view mode (move
          // list isn't otherwise interactive). The fn returns the
          // current map snapshot; null/empty disables the glyph.
          const forkCounts = (evt.payload.view && !editing && forkChildCountsFn)
            ? forkChildCountsFn()
            : null;
          renderMoveList(
            moveListEl, evt.payload.moves_san || [], currentIdx, clickHandler,
            forkCounts, onForkClick,
          );
        }
        setOpening(evt.payload.opening);
        setTablebase(evt.payload.tablebase);
        // View mode: surface PGN-derived eval (white POV) in the engine
        // info panel so scrubbing through the game shows per-ply scores.
        if (showEngineInfo && evt.payload.view) {
          const ev = evt.payload.view.eval;
          const hasAnyEval = !!evt.payload.view.has_eval;
          if (ev) {
            engineSection?.classList.remove("is-empty");
            if (engineScore) engineScore.textContent = fmtScore(ev);
            if (engineDepth) engineDepth.textContent = ev.depth ?? "";
            // Clear live-only fields that have no PGN equivalent.
            if (engineNodes) engineNodes.textContent = "";
            if (engineNps) engineNps.textContent = "";
            if (engineTbhits) engineTbhits.textContent = "";
            if (engineHashfull) engineHashfull.textContent = "";
            if (enginePv) { enginePv.textContent = ""; enginePv.removeAttribute("title"); }
          } else if (!hasAnyEval) {
            // PGN has no eval anywhere -- hide the panel so subsequent
            // imports of bare PGNs don't inherit visibility from a prior
            // import that had evals.
            if (engineScore) engineScore.textContent = "";
            if (engineDepth) engineDepth.textContent = "";
            engineSection?.classList.add("is-empty");
          } else {
            // PGN has evals elsewhere but this specific ply doesn't
            // (e.g. last move of a fastchess game tends to lack an
            // eval). Keep the panel visible so it doesn't disappear
            // when scrubbing across plies, but blank the per-ply
            // fields so stale values from the previous ply don't
            // leak through.
            engineSection?.classList.remove("is-empty");
            if (engineScore) engineScore.textContent = "";
            if (engineDepth) engineDepth.textContent = "";
            if (engineNodes) engineNodes.textContent = "";
            if (engineNps) engineNps.textContent = "";
            if (engineTbhits) engineTbhits.textContent = "";
            if (engineHashfull) engineHashfull.textContent = "";
            if (enginePv) { enginePv.textContent = ""; enginePv.removeAttribute("title"); }
          }
        }
        if (interactive && !editing) board.enableInput(true);
        break;
      case "clock_tick":
        setClock(evt.payload);
        break;
      case "engine_search_start":
        if (!showEngineInfo) break;
        if (engineDepth) engineDepth.textContent = "";
        if (engineScore) engineScore.textContent = "";
        if (engineNodes) engineNodes.textContent = "";
        if (engineNps) engineNps.textContent = "";
        if (engineTbhits) engineTbhits.textContent = "";
        if (engineHashfull) engineHashfull.textContent = "";
        if (enginePv) { enginePv.textContent = ""; enginePv.removeAttribute("title"); }
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
        syncPvVisibility();
        if (!editing && evt.payload.pv_uci && evt.payload.pv_uci.length > 0) {
          const m = evt.payload.pv_uci[0];
          if (m && m.length >= 4) {
            board.setArrow(m.slice(0, 2), m.slice(2, 4));
          }
        }
        break;
      case "game_result":
        if (interactive && !editing) board.enableInput(false);
        if (!editing) board.cancelAnimations();
        break;
    }
  }

  let off = null;
  if (events) {
    off = events.on(applyEvent);
  }

  return {
    ready,
    setGameId(id) {
      gameId = id;
    },
    setHumanWhite,
    setNames,
    applyEvent,
    clearArrows() {
      board.clearArrows();
    },
    setEnabled(enabled) {
      board.enableInput(interactive && enabled);
    },
    reset() {
      // Reset visible game state for a fresh game; the next board_update
      // from the server will set the new starting position.
      board.setPosition(INITIAL_FEN, null);
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
      setFen(INITIAL_FEN);
    },
    enterEditMode(onPositionChange, seed) {
      board.enterEditMode(onPositionChange, seed);
      this.setEditSide(seed?.stm);
    },
    setEditSide(stm) {
      // Authoritative setter for the in-edit STM. Updates clock-active
      // styling immediately since the server isn't ticking during edit.
      editStm = stm === "b" ? "b" : "w";
      if (showClocks) {
        _applyClockActive(editStm === "b" ? "black" : "white", true);
      }
    },
    getEditSide() {
      return editStm;
    },
    exitEditMode() {
      board.exitEditMode();
      // Clear .active so the stale STM highlight doesn't persist past the
      // edit; the next clock_tick from a real board_update re-applies it.
      if (showClocks) _applyClockActive("white", false);
    },
    toggleCastlingRight(right) {
      board.toggleCastlingRight(right);
    },
    getCastlingRights() {
      return board.getCastlingRights();
    },
    getFen() {
      // Full server-emitted FEN (with STM/castling/ep/clocks). The canonical
      // "what does the server think the position is" accessor.
      return currentFen;
    },
    getEditFen() {
      // FEN reflecting the in-flight edit: live piece placement + the
      // user-chosen STM + castling rights. ep/halfmove/fullmove reset
      // because edits forget move history.
      const pieces = board.getPiecePlacement();
      const rights = board.getCastlingRights();
      const castling = [
        rights.wK ? "K" : "",
        rights.wQ ? "Q" : "",
        rights.bK ? "k" : "",
        rights.bQ ? "q" : "",
      ].join("") || "-";
      return `${pieces} ${editStm} ${castling} - 0 1`;
    },
    unmount() {
      editing = false;
      board.destroy();
      off?.();
      try { ro.disconnect(); } catch {}
      window.removeEventListener("resize", recomputeBoardSize);
      window.removeEventListener("sturddle:layout-changed", recomputeBoardSize);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("focus", onWindowFocus);
      if (recomputeRaf) cancelAnimationFrame(recomputeRaf);
    },
  };
}
