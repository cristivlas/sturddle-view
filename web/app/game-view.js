// GameView: board + clocks + move list + engine info, scoped to a gameId.
// Used by Play (interactive, full-size) and Observe (read-only, in a WinBox).

import { mountBoard } from "./board.js";
import { toast } from "./dialogs.js";
import {
  DOCK_DROP_ELIGIBLE_CLASS,
  DOCK_EMPTY_CLASS,
  DOCK_GHOST_SEL,
  DOCK_GRIP_CLASS,
  isMobileLayout,
} from "./play-dock-windows.js";
import { PLAYER_NAME_DEFAULT } from "./settings-dialog.js";
import { APP_EVT } from "./app-events.js";
import { KIND } from "./game-events.js";
import { SIDE, FEN_STM } from "./chess-consts.js";
import { fmtClock, fmtCount, fmtMoveNo, fmtScore, markSelectable, rafCoalesce } from "./wb-utils.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw, saveRaw } from "./storage.js";

const INITIAL_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

// Clock-row name cap: largest value that fits beside the clock on the
// narrowest desktop board (~400px at the 800px viewport breakpoint).
const MAX_CLOCK_NAME_DESKTOP = 48;
const MAX_CLOCK_NAME_MOBILE = 24;

const FEN_COPY_TOAST_MS = 1500;
// 50-move-rule warning: highlight the halfmove clock at/after this many plies.
const HALFMOVE_WARN_PLIES = 40;

// Board/rail sizing. Lengths in px unless suffixed _REM (multiplied by the
// root font-size at use so they honor the user's browser font preference).
const DEFAULT_MAIN_PAD_BOTTOM_PX = 16;
const COL_SIBLING_GAP_PX = 8;
const NESTED_SIBLING_GAP_PX = 12;
const BOTTOM_MARGIN_EXTRA_PX = 24;
const DEFAULT_GRID_GAP_PX = 16;
const DEFAULT_SIDE_PAD_PX = 32;
const DEFAULT_ROOT_FONT_PX = 16;
const MIN_BOARD_REM = 20;     // 320px @ default fs
const RAIL_MIN_REM = 11.25;   // 180px @ default fs
const RAIL_MAX_REM = 20;      // 320px @ default fs
const MIN_AVAIL_REM = 10;
const RAIL_EDGE_GAP_REM = 1; // viewport-edge breathing room beside the rail
const RAIL_WIDTH_FRACTION = 0.18;
const DEFAULT_LEFT_RAIL_EMPTY_RATIO = 0.4;
// Viewport >= this caps the rail at its natural width (raw px on purpose:
// a rem-derived threshold would slide the rail as font-size grows).
const RAIL_NATURAL_CAP_VIEWPORT_PX = 1500;
// Floor for the moves list when the rail-dock grip is dragged up.
const MIN_MOVES_REM = 6;
const RAIL_GRIP_SEL_CLASS = "play-rail-grip";
// Grip thickness; it rides in the board-bottom gap, so it costs neither the
// moves list nor the docked panel any height.
const RAIL_GRIP_PX = 5;

function renderMoveList(el, sanList, {
  currentIdx = null,   // highlighted ply, or null = last (play mode)
  onMoveClick = null,  // (plyIndex) => void; makes cells clickable (view goto)
  forkInfo = null,     // Map<plyIdx, {childCount, isOwnForkPly}> for fork glyphs
  onForkClick = null,  // (plyIdx) => void; glyph-only click re-shows a banner
  boardEl = null,      // mobile: scrolled into view instead of the cursor row
} = {}) {
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
    const info = forkInfo ? forkInfo.get(plyIdx) : null;
    const childCount = info?.childCount ?? 0;
    const isOwnForkPly = !!info?.isOwnForkPly;
    if (childCount > 0 || isOwnForkPly) {
      cell.classList.add("has-fork");
      const glyph = document.createElement("wa-icon");
      glyph.setAttribute("name", "code-fork");
      glyph.className = "fork-glyph";
      // Tooltip: children-here wins when present; otherwise show the
      // child-side "forked from parent" label.
      glyph.title = childCount > 0
        ? (childCount === 1
            ? "1 variation from this position"
            : `${childCount} variations from this position`)
        : "Forked from parent here";
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
    num.textContent = fmtMoveNo(i, true);
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
  // Mobile has no inner scroller (the list flows in the page), so scrolling
  // the cursor into view would drag the board off-screen -- the board is what
  // the user is scrubbing, so keep that in view instead.
  if (currentIdx == null) {
    el.scrollTop = el.scrollHeight;
  } else if (isMobileLayout()) {
    boardEl?.scrollIntoView({ block: "nearest" });
  } else if (highlightedRow) {
    highlightedRow.scrollIntoView({ block: "nearest" });
  }
}

// Unicode ellipsis is intentional: this glyph is rendered into the
// clock-name span (user-facing), not a code token.
function truncName(s) {
  if (!s) return s;
  const max = isMobileLayout() ? MAX_CLOCK_NAME_MOBILE : MAX_CLOCK_NAME_DESKTOP;
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

// ---- FEN / opening / tablebase lines ------------------------------------

function setFen(ctx, fen) {
  ctx.currentFen = fen || "";
  if (ctx.fenText) ctx.fenText.textContent = ctx.currentFen;
}

async function copyFen(ctx) {
  if (!ctx.currentFen) return;
  // Prefer the async Clipboard API (works on https + localhost). Fall
  // back to the legacy execCommand path for plain-http hosts where the
  // async API is blocked.
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(ctx.currentFen);
    } else {
      const ta = document.createElement("textarea");
      ta.value = ctx.currentFen;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      if (!ok) throw new Error("execCommand failed");
    }
    toast("FEN copied", { variant: "success", duration: FEN_COPY_TOAST_MS });
  } catch {
    toast("Could not copy FEN", { variant: "danger" });
  }
}

function setOpening(ctx, opening) {
  if (!ctx.openingLine) return;
  if (!opening || (!opening.eco && !opening.name)) {
    ctx.openingLine.classList.add("is-empty");
    return;
  }
  ctx.openingEco.textContent = opening.eco ?? "";
  ctx.openingName.textContent = opening.name ?? "";
  ctx.openingLine.classList.remove("is-empty");
}

function setTablebase(ctx, tb) {
  if (!ctx.tbLine) return;
  const hm = tb && Number.isFinite(tb.halfmove_clock) ? tb.halfmove_clock : null;
  const hasTb = tb && tb.wdl !== undefined && tb.wdl !== null;
  if (!hasTb && hm === null) {
    ctx.tbLine.classList.add("is-empty");
    return;
  }
  if (ctx.tbInfo) {
    if (hasTb) {
      const wdl = ({ 2: "Win", 1: "Cursed win", 0: "Draw", "-1": "Blessed loss", "-2": "Loss" })[tb.wdl] ?? "--";
      let s = hm !== null ? ` · ${wdl}` : wdl;
      if (Number.isFinite(tb.dtz)) s += ` · DTZ ${tb.dtz}`;
      if (Number.isFinite(tb.dtm)) s += ` · DTM ${tb.dtm}`;
      if (tb.best) s += ` · ${tb.best}`;
      ctx.tbInfo.textContent = s;
    } else {
      ctx.tbInfo.textContent = "";
    }
  }
  if (ctx.hmClock) {
    ctx.hmClock.textContent = hm !== null ? `50-move rule: ${hm}/100` : "";
    ctx.hmClock.classList.toggle("hm-clock-warn", hm !== null && hm >= HALFMOVE_WARN_PLIES);
  }
  ctx.tbLine.classList.remove("is-empty");
}

// ---- Names / clocks -----------------------------------------------------

// PV row hides only in pure view mode (navigating an imported game
// with no engine running). Play mode and analysis mode both produce
// a meaningful PV.
function syncPvVisibility(ctx) {
  const hidePv = ctx.viewing && !ctx.analyzing;
  if (ctx.enginePv) ctx.enginePv.classList.toggle("hidden", hidePv);
  if (ctx.engineSection) ctx.engineSection.classList.toggle("no-pv", hidePv);
}

function setNames(ctx, { top, bottom } = {}) {
  if (top !== undefined) {
    if (ctx.clockTopName) {
      ctx.clockTopName.textContent = truncName(top);
      ctx.clockTopName.title = top || "";
    }
  }
  if (bottom !== undefined) {
    if (ctx.clockBottomName) {
      ctx.clockBottomName.textContent = truncName(bottom);
      ctx.clockBottomName.title = bottom || "";
    }
  }
}

function setHumanWhite(ctx, value) {
  ctx.humanWhite = !!value;
  ctx.board.setSide(ctx.humanWhite ? SIDE.WHITE : SIDE.BLACK);
  // In interactive (Play) mode, bottom = human, top = engine. In view
  // mode, re-swap cached PGN names to match the new orientation.
  if (ctx.interactive && !ctx.viewing) {
    setNames(ctx, { bottom: ctx.playerName, top: ctx.engineName });
  } else if (ctx.viewing && ctx.viewWhiteName !== null) {
    if (ctx.humanWhite) setNames(ctx, { bottom: ctx.viewWhiteName, top: ctx.viewBlackName });
    else setNames(ctx, { bottom: ctx.viewBlackName, top: ctx.viewWhiteName });
  }
  // Re-apply clock colors and active state: clock_tick won't fire until
  // the next server event, so do it eagerly here for both edit and view.
  if (ctx.showClocks) {
    applyClockColors(ctx);
    if (ctx.editing) {
      applyClockActive(ctx, ctx.editStm === FEN_STM.BLACK ? SIDE.BLACK : SIDE.WHITE, true);
    } else {
      applyClockActive(ctx, ctx.lastTurn, ctx.lastClockRunning);
    }
  }
}

function bottomIsWhite(ctx) {
  // In Observe (non-interactive) the bottom row is always white. In Play
  // the bottom is the human's side.
  return ctx.interactive ? ctx.humanWhite : true;
}

function applyClockActive(ctx, turn, active) {
  const bottomWhite = bottomIsWhite(ctx);
  const bottomToMove =
    (turn === SIDE.WHITE && bottomWhite) || (turn === SIDE.BLACK && !bottomWhite);
  ctx.clockBottomRow?.classList.toggle("active", active && bottomToMove);
  ctx.clockTopRow?.classList.toggle("active", active && !bottomToMove);
}

function applyClockColors(ctx) {
  if (!ctx.showClocks) return;
  const bottomWhite = bottomIsWhite(ctx);
  if (ctx.clockBottomRow) ctx.clockBottomRow.dataset.color = bottomWhite ? SIDE.WHITE : SIDE.BLACK;
  if (ctx.clockTopRow) ctx.clockTopRow.dataset.color = bottomWhite ? SIDE.BLACK : SIDE.WHITE;
}

// `viewing` here is the event's clock flag, distinct from ctx.viewing.
function setClock(ctx, { white_time, black_time, turn, running, viewing }) {
  if (!ctx.showClocks) return;
  ctx.lastTurn = turn || SIDE.WHITE;
  ctx.lastClockRunning = running || !!viewing;
  const bottomWhite = bottomIsWhite(ctx);
  const bottomTime = bottomWhite ? white_time : black_time;
  const topTime = bottomWhite ? black_time : white_time;
  if (ctx.clockBottomTime) ctx.clockBottomTime.textContent = fmtClock(bottomTime);
  if (ctx.clockTopTime) ctx.clockTopTime.textContent = fmtClock(topTime);
  applyClockColors(ctx);
  applyClockActive(ctx, ctx.lastTurn, ctx.lastClockRunning);
}

function clearEngineInfoFields(ctx) {
  if (ctx.engineDepth) ctx.engineDepth.textContent = "";
  if (ctx.engineScore) ctx.engineScore.textContent = "";
  if (ctx.engineNodes) ctx.engineNodes.textContent = "";
  if (ctx.engineNps) ctx.engineNps.textContent = "";
  if (ctx.engineTbhits) ctx.engineTbhits.textContent = "";
  if (ctx.engineHashfull) ctx.engineHashfull.textContent = "";
  if (ctx.enginePv) { ctx.enginePv.textContent = ""; ctx.enginePv.removeAttribute("title"); }
}

// ---- Board sizing -------------------------------------------------------

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

function readMainPaddingBottom() {
  const main = document.querySelector("main");
  if (!main) return DEFAULT_MAIN_PAD_BOTTOM_PX;
  const v = parseFloat(getComputedStyle(main).paddingBottom);
  return Number.isFinite(v) ? v : DEFAULT_MAIN_PAD_BOTTOM_PX;
}

function clearFixedGeom(el) {
  if (!el) return;
  for (const prop of ["left", "top", "width", "height"]) el.style.removeProperty(prop);
}

// Rail-dock lift: how far the docked window's top edge is dragged above the
// board bottom, stealing that height from the moves list. Shared by every
// GameView instance (only one is on a desktop play grid at a time).
let railLift = Math.max(0, Number(loadRaw(STORAGE_KEY.PLAY_RAIL_LIFT, 0)) || 0);

// Grip between the moves list and whatever is docked in the rail band. It is
// the band's fixed-positioned sibling, riding in the board-bottom gap above
// it -- inside the band it would sit within the panel's border.
function ensureRailGrip(ctx, railDock) {
  const existing = railDock.parentElement?.querySelector(`.${RAIL_GRIP_SEL_CLASS}`);
  if (existing) return existing;
  const grip = document.createElement("div");
  grip.className = `${DOCK_GRIP_CLASS} ${RAIL_GRIP_SEL_CLASS}`;
  railDock.after(grip);
  grip.addEventListener("pointerdown", (eDown) => {
    if (eDown.button !== 0) return;
    eDown.preventDefault();
    try { grip.setPointerCapture(eDown.pointerId); } catch { /* */ }
    grip.classList.add("dragging");
    const lift0 = railLift;
    const y0 = eDown.clientY;
    const onMove = (e) => {
      railLift = Math.max(0, Math.min(lift0 - (e.clientY - y0), ctx.railMaxLift ?? 0));
      if (ctx.railGeom) positionSideRail(ctx, ctx.railGeom);
    };
    const onUp = () => {
      grip.classList.remove("dragging");
      grip.removeEventListener("pointermove", onMove);
      grip.removeEventListener("pointerup", onUp);
      grip.removeEventListener("pointercancel", onUp);
      saveRaw(STORAGE_KEY.PLAY_RAIL_LIFT, String(Math.round(railLift)));
    };
    grip.addEventListener("pointermove", onMove);
    grip.addEventListener("pointerup", onUp);
    grip.addEventListener("pointercancel", onUp);
  });
  return grip;
}

// Position the side rail (moves/engine panel) flush with the board on
// desktop; clear inline geometry on mobile so the flex layout takes over.
function positionSideRail(ctx, geom) {
  const { grid, gapW, railW, leftEmpty, rem, mobile } = geom;
  const sideHost = grid.querySelector(".play-side-host");
  if (!sideHost) return;
  const railDock = sideHost.querySelector(".play-rail-dock");
  if (mobile) {
    clearFixedGeom(sideHost);
    sideHost.style.removeProperty("margin-top");
    if (railDock) clearFixedGeom(railDock);
    clearFixedGeom(sideHost.querySelector(`.${RAIL_GRIP_SEL_CLASS}`));
    return;
  }
  const boardRect = ctx.boardEl.getBoundingClientRect();
  const ribbonRight = document.body.dataset.ribbonSide === "right";
  // Align the rail's top with the top clock row when the engine-stat section
  // is showing above the moves list, so its header lines up with the clock
  // area. With no engine section (or it's empty/hidden) the moves list is
  // the rail's only content, so keep it flush with the board top instead.
  const clockTopRow = ctx.clockTopRow;
  const topRef = clockTopRow && clockTopRow.offsetParent !== null
    && !!ctx.engineSection && ctx.engineSection.offsetParent !== null
    ? clockTopRow.getBoundingClientRect()
    : boardRect;
  const top = Math.ceil(topRef.top);
  // Cap the rail at its natural width only on wide viewports with an
  // empty dock side (keeps the picture centered). A visible docker
  // lets the rail fill `avail` at any width.
  const capRail = leftEmpty && window.innerWidth >= RAIL_NATURAL_CAP_VIEWPORT_PX;
  // ribbonRight: rail sits left of the board; else it sits right.
  // Both fill `avail` (capped to railW on wide+empty), differing
  // only in which board edge the rail hangs off of.
  let left;
  let width;
  if (ribbonRight) {
    const avail = Math.max(0, Math.ceil(boardRect.left) - gapW - rem(RAIL_EDGE_GAP_REM));
    width = capRail ? Math.min(railW, avail) : avail;
    left = Math.max(rem(RAIL_EDGE_GAP_REM), Math.ceil(boardRect.left) - gapW - width);
  } else {
    left = Math.ceil(boardRect.right) + gapW;
    const avail = Math.max(0, window.innerWidth - left - rem(RAIL_EDGE_GAP_REM));
    width = capRail ? Math.min(railW, avail) : avail;
  }
  const height = Math.max(rem(MIN_AVAIL_REM), Math.floor(boardRect.bottom - topRef.top));
  // Lift applies only while something is docked in the rail band; with an
  // empty band the moves list runs all the way down to the board bottom.
  ctx.railMaxLift = Math.max(0, height - rem(MIN_MOVES_REM));
  // A pending drop counts as an occupant, so the drop outline (and the hover
  // preview) shows the band at its persisted, lifted size, not the bare one.
  const occupied = !!railDock && (
    !railDock.classList.contains(DOCK_EMPTY_CLASS)
    || railDock.classList.contains(DOCK_DROP_ELIGIBLE_CLASS)
    || !!railDock.querySelector(DOCK_GHOST_SEL));
  const lift = occupied ? Math.min(railLift, ctx.railMaxLift) : 0;
  sideHost.style.left = `${left}px`;
  sideHost.style.top = `${top}px`;
  sideHost.style.width = `${width}px`;
  sideHost.style.height = `${height - lift}px`;
  sideHost.style.removeProperty("margin-top");
  // The rail dock is purely additive: a fixed band under the moves box
  // (same x as the rail) filling the gap from the board bottom down to the
  // clock bottom. It never joins the rail's flex flow, so the moves list
  // keeps its exact geometry.
  if (railDock) {
    const boardBottom = Math.floor(boardRect.bottom);
    const clockRow = ctx.clockBottomRow;
    const barBottom = clockRow && clockRow.offsetParent !== null
      ? Math.floor(clockRow.getBoundingClientRect().bottom)
      : boardBottom;
    // Band starts one grip-thickness below the board, so the grip fills the
    // gap between the moves list and the docked panel exactly.
    const barTop = boardBottom + RAIL_GRIP_PX - lift;
    railDock.style.left = `${left}px`;
    railDock.style.top = `${barTop}px`;
    railDock.style.width = `${width}px`;
    railDock.style.height = `${Math.max(0, barBottom - barTop)}px`;
    const grip = ensureRailGrip(ctx, railDock);
    grip.style.display = occupied ? "" : "none";
    grip.style.left = `${left}px`;
    grip.style.top = `${barTop - RAIL_GRIP_PX}px`;
    grip.style.width = `${width}px`;
    grip.style.height = `${RAIL_GRIP_PX}px`;
    ctx.railGeom = geom;
  }
}

function recomputeNow(ctx) {
  const { boardCol, boardEl, board } = ctx;
  if (!boardCol) return;
  boardEl.style.width = "0";
  const colRect = boardCol.getBoundingClientRect();

  let siblingsInCol = 0;
  for (const child of boardCol.children) {
    if (child === boardEl) continue;
    if (child.offsetParent === null) continue;
    siblingsInCol += child.getBoundingClientRect().height + COL_SIBLING_GAP_PX;
  }

  let belowGameView = 0;
  let node = boardCol;
  while (node?.parentElement && node !== document.body) {
    belowGameView += sumSiblingsBelow(node, NESTED_SIBLING_GAP_PX);
    const parent = node.parentElement;
    if (parent.id === "play-perspective" || parent.tagName === "MAIN") break;
    node = parent;
  }

  const bottomMargin = readMainPaddingBottom() + BOTTOM_MARGIN_EXTRA_PX;
  const availH = Math.max(
    0,
    window.innerHeight - colRect.top - siblingsInCol - belowGameView - bottomMargin
  );

  // Layout (wide viewports): [left-filler][board][rail], where the rail
  // and the left filler are the same width, so the board sits dead-center
  // horizontally. Rail width is viewport-driven (not board-driven) to
  // avoid a feedback loop with the board sizing below.
  // Rail/board minimums are derived from root font-size so they honor
  // the user's browser font-size preference. Mobile branch is gated by
  // isMobileLayout() so a short-but-wide viewport (height breakpoint)
  // also clears the desktop rail positioning instead of stranding the
  // side rail at fixed coordinates.
  const rootFs = parseFloat(getComputedStyle(document.documentElement).fontSize) || DEFAULT_ROOT_FONT_PX;
  const rem = (n) => Math.round(n * rootFs);
  const MIN_BOARD = rem(MIN_BOARD_REM);
  const RAIL_MIN = rem(RAIL_MIN_REM);
  const RAIL_MAX = rem(RAIL_MAX_REM);
  const grid = boardCol.closest(".play-grid") || boardCol.closest("#play-perspective");
  const gridStyle = grid ? getComputedStyle(grid) : null;
  const gapW = gridStyle
    ? parseFloat(gridStyle.getPropertyValue("--grid-gap")) || DEFAULT_GRID_GAP_PX
    : DEFAULT_GRID_GAP_PX;
  const main = document.querySelector("main");
  const mainStyle = main ? getComputedStyle(main) : null;
  const sidePad = mainStyle
    ? parseFloat(mainStyle.paddingLeft) + parseFloat(mainStyle.paddingRight)
    : DEFAULT_SIDE_PAD_PX;

  let railW;
  // leftRailW / --left-rail-w name the dock-side column, not a screen side:
  // when ribbon_side="right", the mirror swaps grid columns so this width
  // applies to the right rail. Shrink-when-empty still tracks the dock.
  let leftRailW;
  let availW;
  // Whether the dock side has no docker visible. Defaults true (mobile
  // branch never reads it for sizing); set in the desktop branch below.
  let leftEmpty = true;
  // When the left dock is empty on desktop, shrink the left rail so the
  // board + right rail shift left as one block instead of being framed
  // by a wide empty band. Proportional to railW so it scales with width.
  const LEFT_RAIL_EMPTY_RATIO = window.__leftRailEmptyRatio ?? DEFAULT_LEFT_RAIL_EMPTY_RATIO;
  if (isMobileLayout() || !grid) {
    railW = 0;
    leftRailW = 0;
    availW = Math.max(rem(MIN_AVAIL_REM), Math.floor(colRect.width));
  } else {
    const usable = window.innerWidth - sidePad;
    railW = Math.max(RAIL_MIN, Math.min(RAIL_MAX, Math.floor(usable * RAIL_WIDTH_FRACTION)));
    leftEmpty =
      document.querySelector(".play-dock-left")?.classList.contains("dock-empty") !== false
      && document.querySelector(".play-comments-host")?.classList.contains("dock-empty") !== false;
    leftRailW = leftEmpty ? Math.floor(railW * LEFT_RAIL_EMPTY_RATIO) : railW;
    availW = Math.max(rem(MIN_AVAIL_REM), Math.floor(usable - railW - leftRailW - 2 * gapW));
  }

  const max = isMobileLayout()
    ? availW
    // Honor the CSS minmax(320px, ...) floor so the board doesn't go
    // below MIN_BOARD on awkward width-bound viewports (~800-900px).
    : Math.max(MIN_BOARD, Math.floor(Math.min(availW, availH)));

  boardEl.style.width = `${max}px`;
  const inner = boardEl.firstElementChild;
  if (inner) {
    inner.style.width = `${max}px`;
    inner.style.height = `${max}px`;
  }
  // Publish the computed board width so siblings (clocks, opening line,
  // controls bar) can clamp to the same width -- and so the grid's
  // board column shrinks to that width, gluing the side rail next to it.
  boardCol.style.setProperty("--board-max-px", `${max}px`);
  if (grid) {
    grid.style.setProperty("--board-max-px", `${max}px`);
    // Only drive the column width on desktop; in mobile layout
    // (narrow width OR short height -- see media query in styles.css)
    // the grid collapses to a vertical flex layout.
    const mobile = isMobileLayout();
    if (!mobile) {
      grid.style.setProperty("--board-col-px", `${max}px`);
      grid.style.setProperty("--left-rail-w", `${leftRailW}px`);
    } else {
      grid.style.removeProperty("--board-col-px");
      grid.style.removeProperty("--left-rail-w");
    }
    // Align the side rail's top with the board's top (the grid would
    // otherwise place it next to the top clock row), and set its height
    // to the board's so the moves panel fills down to the board bottom.
    positionSideRail(ctx, { grid, gapW, railW, leftEmpty, mobile, rem });
  }
  board.forceResize();
}

function recomputeBoardSize(ctx) {
  ctx.scheduleRecompute ??= rafCoalesce(() => {
    recomputeNow(ctx);
    // Run again after the next paint so secondary measurements reflect
    // the new layout (e.g. controls/clocks settled into final positions).
    requestAnimationFrame(() => recomputeNow(ctx));
  });
  ctx.scheduleRecompute();
}

// ---- Event handlers -----------------------------------------------------

function applyBoardUpdate(ctx, evt) {
  const { board } = ctx;
  ctx.viewing = !!evt.payload.view;
  if (typeof evt.payload.editing === "boolean") {
    const wasEditing = ctx.editing;
    ctx.editing = evt.payload.editing;
    if (ctx.editing && !wasEditing) board.clearArrows();
  }
  if (typeof evt.payload.analyzing === "boolean") {
    ctx.analyzing = evt.payload.analyzing;
  }
  syncPvVisibility(ctx);
  if (evt.payload.engine_name) {
    ctx.engineName = evt.payload.engine_name;
    if (ctx.interactive) setNames(ctx, { top: ctx.engineName });
  }
  if (evt.payload.player_name) {
    ctx.playerName = evt.payload.player_name;
    if (ctx.interactive && !ctx.viewing) setNames(ctx, { bottom: ctx.playerName });
  }
  if (typeof evt.payload.human_white === "boolean") {
    ctx.humanWhite = evt.payload.human_white;
    board.setSide(ctx.humanWhite ? SIDE.WHITE : SIDE.BLACK);
    if (ctx.interactive) setNames(ctx, { bottom: ctx.playerName, top: ctx.engineName });
  }
  // View mode: surface the PGN's player names instead of Human/engine.
  if (ctx.interactive && evt.payload.view) {
    const w = evt.payload.view.white_name || "White";
    const b = evt.payload.view.black_name || "Black";
    ctx.viewWhiteName = w;
    ctx.viewBlackName = b;
    // Bottom is white when not flipped (humanWhite acts as the orient
    // toggle even in view mode).
    if (ctx.humanWhite) setNames(ctx, { bottom: w, top: b });
    else setNames(ctx, { bottom: b, top: w });
  }
  // Skip setPosition during edit so the user's in-progress board edits
  // aren't clobbered by server state. Exception: cold mount mid-edit
  // (firstBoardUpdate) -- there are no in-progress edits yet, and the
  // board is at the cm-chessboard default startpos; we must seed it from
  // the server's authoritative FEN.
  if (!ctx.editing || ctx.firstBoardUpdate) {
    // Live update overrides any AI preview; drop the preview flag so
    // input lock and stale restore-target don't linger.
    if (ctx.previewActive) {
      ctx.previewActive = false;
      board.enableInput(ctx.previewInputWasEnabled);
    }
    // Suppress animation when the incoming FEN matches the current one.
    // cm-chessboard otherwise re-runs its 200ms animation queue on a
    // no-op move (visible flicker), e.g. when x-game nav opens the parent
    // at the same fork ply.
    const sameFen = !ctx.firstBoardUpdate && ctx.currentFen === evt.payload.fen;
    const animate = !ctx.firstBoardUpdate && !sameFen;
    if (ctx.boardHold) {
      // Held (optimistic move in flight over a stop/resume-analysis
      // round trip): don't touch the pieces -- a stale pre-move echo
      // would snap the dropped piece back. releaseBoard() reconciles.
      ctx.holdPending = { fen: evt.payload.fen, lastMove: evt.payload.last_move };
    } else {
      board.setPosition(evt.payload.fen, evt.payload.last_move, animate);
    }
  }
  if (ctx.firstBoardUpdate) {
    ctx.firstBoardUpdate = false;
    ctx.resolveReady();
  }
  setFen(ctx, evt.payload.fen);
  if (!ctx.editing) board.clearArrows();
  if (ctx.showMoves && ctx.moveListEl) {
    // View mode highlights the cursor's ply (cursor-1 = last played move;
    // cursor=0 means initial position -> no highlight) and lets the user
    // jump by clicking a move in the list.
    let currentIdx = null;
    let clickHandler = null;
    if (evt.payload.view && !ctx.editing) {
      currentIdx = (evt.payload.view.cursor ?? 0) - 1;
      // No ply-jump (and no clickable cursor) while analyzing.
      if (!ctx.analyzing) clickHandler = ctx.onMoveJump;
    } else if (!ctx.editing && !ctx.analyzing && ctx.onPlayMoveClick) {
      // Play mode: clicking a past move flips into server view mode at
      // that ply (the handler ignores clicks on the live last move).
      clickHandler = ctx.onPlayMoveClick;
    }
    // Fork glyphs only in view mode; snapshot at render time.
    const forkInfo = (evt.payload.view && !ctx.editing && ctx.forkInfoFn)
      ? ctx.forkInfoFn()
      : null;
    renderMoveList(ctx.moveListEl, evt.payload.moves_san || [], {
      currentIdx,
      onMoveClick: clickHandler,
      forkInfo,
      onForkClick: ctx.onForkClick,
      boardEl: ctx.boardEl,
    });
  }
  setOpening(ctx, evt.payload.opening);
  setTablebase(ctx, evt.payload.tablebase);
  if (ctx.showEngineInfo && evt.payload.view) applyViewEval(ctx, evt);
  if (ctx.interactive && !ctx.editing) board.enableInput(true);
}

// View mode: surface PGN-derived eval (white POV) in the engine info
// panel so scrubbing through the game shows per-ply scores.
function applyViewEval(ctx, evt) {
  const ev = evt.payload.view.eval;
  const hasAnyEval = !!evt.payload.view.has_eval;
  if (ev) {
    ctx.engineSection?.classList.remove("is-empty");
    clearEngineInfoFields(ctx);
    if (ctx.engineScore) ctx.engineScore.textContent = fmtScore(ev, { signed: true });
    if (ctx.engineDepth) ctx.engineDepth.textContent = ev.depth ?? "";
  } else if (!hasAnyEval) {
    // PGN has no eval anywhere -- hide the panel so subsequent imports of
    // bare PGNs don't inherit visibility from a prior import that had evals.
    if (ctx.engineScore) ctx.engineScore.textContent = "";
    if (ctx.engineDepth) ctx.engineDepth.textContent = "";
    ctx.engineSection?.classList.add("is-empty");
  } else {
    // PGN has evals elsewhere but this specific ply doesn't (e.g. last
    // move of a fastchess game tends to lack an eval). Keep the panel
    // visible so it doesn't disappear when scrubbing across plies, but
    // blank the per-ply fields so stale values don't leak through.
    ctx.engineSection?.classList.remove("is-empty");
    clearEngineInfoFields(ctx);
  }
}

function applyEngineInfo(ctx, evt) {
  ctx.engineSection?.classList.remove("is-empty");
  if (ctx.engineDepth && evt.payload.depth != null) {
    ctx.engineDepth.textContent = evt.payload.depth;
  }
  if (ctx.engineScore && evt.payload.score) {
    ctx.engineScore.textContent = fmtScore(evt.payload.score, { signed: true });
  }
  if (ctx.engineNodes && evt.payload.nodes != null) {
    ctx.engineNodes.textContent = fmtCount(evt.payload.nodes);
  }
  if (ctx.engineNps && evt.payload.nps != null) {
    ctx.engineNps.textContent = fmtCount(evt.payload.nps);
  }
  if (ctx.engineTbhits) {
    ctx.engineTbhits.textContent = evt.payload.tbhits ? fmtCount(evt.payload.tbhits) : "";
  }
  if (ctx.engineHashfull && evt.payload.hashfull != null) {
    ctx.engineHashfull.textContent = `${(evt.payload.hashfull / 10).toFixed(0)}%`;
  }
  if (ctx.enginePv && evt.payload.pv && evt.payload.pv.length > 0) {
    const full = evt.payload.pv.join(" ");
    ctx.enginePv.textContent = full;
    ctx.enginePv.setAttribute("title", full);
  }
  syncPvVisibility(ctx);
  if (!ctx.editing && evt.payload.pv_uci && evt.payload.pv_uci.length > 0) {
    const m = evt.payload.pv_uci[0];
    if (m && m.length >= 4) {
      ctx.board.setArrow(m.slice(0, 2), m.slice(2, 4));
    }
  }
}

function applyEvent(ctx, evt) {
  if (!evt) return;
  if (ctx.gameId !== null && evt.game_id && evt.game_id !== ctx.gameId) return;
  switch (evt.kind) {
    case KIND.BOARD_UPDATE:
      applyBoardUpdate(ctx, evt);
      break;
    case KIND.CLOCK_TICK:
      setClock(ctx, evt.payload);
      break;
    case KIND.ENGINE_SEARCH_START:
      if (ctx.showEngineInfo) clearEngineInfoFields(ctx);
      break;
    case KIND.ENGINE_INFO:
      if (ctx.showEngineInfo) applyEngineInfo(ctx, evt);
      break;
    case KIND.AI_RECOMMENDATION:
      if (!ctx.editing && evt.payload.uci && evt.payload.uci.length >= 4) {
        const u = evt.payload.uci;
        ctx.board.setRecommendArrow(u.slice(0, 2), u.slice(2, 4));
      }
      break;
    case KIND.GAME_RESULT:
      if (ctx.interactive && !ctx.editing) ctx.board.enableInput(false);
      if (!ctx.editing) ctx.board.cancelAnimations();
      break;
  }
}

// FEN reflecting the in-flight edit: live piece placement + the
// user-chosen STM + castling rights. ep/halfmove/fullmove reset because
// edits forget move history.
function computeEditFen(ctx) {
  const pieces = ctx.board.getPiecePlacement();
  const rights = ctx.board.getCastlingRights();
  const castling = [
    rights.wK ? "K" : "",
    rights.wQ ? "Q" : "",
    rights.bK ? "k" : "",
    rights.bQ ? "q" : "",
  ].join("") || "-";
  return `${pieces} ${ctx.editStm} ${castling} - 0 1`;
}

// ---- Markup -------------------------------------------------------------

function boardHTML(showClocks) {
  return `
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
}

function sideHTML(showEngineInfo, showMoves) {
  return `
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
}

function queryRefs(ctx, container, sideHost) {
  ctx.boardEl = container.querySelector(".board");
  ctx.clockTopName = container.querySelector('[data-side="top"]');
  ctx.clockTopTime = container.querySelector('[data-time="top"]');
  ctx.clockBottomName = container.querySelector('[data-side="bottom"]');
  ctx.clockBottomTime = container.querySelector('[data-time="bottom"]');
  ctx.clockTopRow = container.querySelector(".clock-top");
  ctx.clockBottomRow = container.querySelector(".clock-bottom");
  ctx.moveListEl = sideHost.querySelector(".move-list");
  ctx.engineDepth = sideHost.querySelector(".engine-depth");
  ctx.engineScore = sideHost.querySelector(".engine-score");
  ctx.engineNodes = sideHost.querySelector(".engine-nodes");
  ctx.engineNps = sideHost.querySelector(".engine-nps");
  ctx.engineTbhits = sideHost.querySelector(".engine-tbhits");
  ctx.engineHashfull = sideHost.querySelector(".engine-hashfull");
  ctx.engineSection = sideHost.querySelector(".game-view-engine");
  ctx.enginePv = sideHost.querySelector(".engine-pv");
  ctx.openingLine = container.querySelector(".opening-line");
  ctx.openingEco = container.querySelector(".opening-eco");
  ctx.openingName = container.querySelector(".opening-name");
  ctx.tbLine = container.querySelector(".tablebase-line");
  ctx.tbInfo = container.querySelector(".tb-info");
  ctx.hmClock = container.querySelector(".hm-clock");
  ctx.fenText = container.querySelector(".fen-text");
  ctx.fenCopyBtn = container.querySelector(".fen-copy");
}

function buildViewApi(ctx) {
  return {
    ready: ctx.ready,
    setGameId(id) { ctx.gameId = id; },
    setHumanWhite: (v) => setHumanWhite(ctx, v),
    setNames: (n) => setNames(ctx, n),
    applyEvent: (evt) => applyEvent(ctx, evt),
    previewPosition(fen, { animate = true } = {}) {
      if (!fen || fen === ctx.currentFen) return;
      if (!ctx.previewActive) ctx.previewInputWasEnabled = ctx.board.isInputEnabled();
      ctx.previewActive = true;
      ctx.board.enableInput(false);
      ctx.board.setPosition(fen, null, animate);
    },
    restorePosition({ animate = true } = {}) {
      if (!ctx.previewActive) return;
      ctx.previewActive = false;
      ctx.board.setPosition(ctx.currentFen, null, animate);
      ctx.board.enableInput(ctx.previewInputWasEnabled);
    },
    // Hold piece rendering across a stop-analysis/resume/move round trip:
    // board_updates still apply (FEN text, move list, ...) but pieces
    // don't, so the stale pre-move echo from stop_analysis can't snap an
    // optimistically-dropped piece back mid-move.
    holdBoard() {
      if (ctx.boardHold) return;
      ctx.boardHold = true;
      ctx.holdPending = null;
      ctx.holdBaseFen = ctx.currentFen;
    },
    // `snap: true` (move rejected) discards the hold and forces the
    // pieces back to the server position immediately.
    releaseBoard({ snap = false } = {}) {
      const pending = ctx.holdPending;
      const baseFen = ctx.holdBaseFen;
      ctx.boardHold = false;
      ctx.holdPending = null;
      ctx.holdBaseFen = null;
      if (snap) {
        ctx.board.setPosition(ctx.currentFen, null, false);
      } else if (pending && pending.fen !== baseFen) {
        // A real position change landed while held (the move's own
        // update raced the release); apply it. Stale same-FEN echoes
        // are dropped -- that's the whole point of the hold.
        ctx.board.setPosition(pending.fen, pending.lastMove, true);
      }
    },
    clearArrows() { ctx.board.clearArrows(); },
    clearEngineInfo() {
      clearEngineInfoFields(ctx);
      ctx.engineSection?.classList.add("is-empty");
    },
    setEnabled(enabled) { ctx.board.enableInput(ctx.interactive && enabled); },
    reset() {
      // Reset visible game state for a fresh game; the next board_update
      // from the server will set the new starting position. Snap (no
      // animation): an instant engine first move (opening book) can land
      // its board_update before this runs, and an animated reset would
      // then slide the board *backward* off that move ("withdrawing" it).
      ctx.board.setPosition(INITIAL_FEN, null, false);
      if (ctx.moveListEl) ctx.moveListEl.innerHTML = "";
      clearEngineInfoFields(ctx);
      ctx.engineSection?.classList.add("is-empty");
      setOpening(ctx, null);
      setTablebase(ctx, null);
      setFen(ctx, INITIAL_FEN);
    },
    enterEditMode(onPositionChange, seed) {
      ctx.board.enterEditMode(onPositionChange, seed);
      this.setEditSide(seed?.stm);
    },
    setEditSide(stm) {
      // Authoritative setter for the in-edit STM. Updates clock-active
      // styling immediately since the server isn't ticking during edit.
      ctx.editStm = stm === FEN_STM.BLACK ? FEN_STM.BLACK : FEN_STM.WHITE;
      if (ctx.showClocks) {
        applyClockActive(ctx, ctx.editStm === FEN_STM.BLACK ? SIDE.BLACK : SIDE.WHITE, true);
      }
    },
    getEditSide() { return ctx.editStm; },
    exitEditMode() {
      ctx.board.exitEditMode();
      // Clear .active so the stale STM highlight doesn't persist past the
      // edit; the next clock_tick from a real board_update re-applies it.
      if (ctx.showClocks) applyClockActive(ctx, SIDE.WHITE, false);
    },
    toggleCastlingRight(right) { ctx.board.toggleCastlingRight(right); },
    getCastlingRights() { return ctx.board.getCastlingRights(); },
    getFen() {
      // Full server-emitted FEN (with STM/castling/ep/clocks). The canonical
      // "what does the server think the position is" accessor.
      return ctx.currentFen;
    },
    getEditFen() { return computeEditFen(ctx); },
    unmount() {
      ctx.editing = false;
      ctx.board.destroy();
      ctx.off?.();
      try { ctx.ro.disconnect(); } catch {}
      window.removeEventListener("resize", ctx.onRecompute);
      window.removeEventListener(APP_EVT.LAYOUT_CHANGED, ctx.onRecompute);
      document.removeEventListener("visibilitychange", ctx.onVisibilityChange);
      window.removeEventListener("focus", ctx.onWindowFocus);
      ctx.scheduleRecompute?.cancel();
    },
  };
}

export function mountGameView(container, opts = {}) {
  const {
    events,
    onMove,
    onMoveJump = null, // view-mode click on a move; (plyIndex) => void
    onPlayMoveClick = null, // play-mode click on a past move; (plyIndex) => void
    forkInfoFn = null, // () => Map<plyIdx, {childCount, isOwnForkPly}>
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
  container.innerHTML = boardHTML(showClocks);
  const sideHost = sideContainer ?? container;
  if (sideContainer) {
    sideContainer.innerHTML = sideHTML(showEngineInfo, showMoves);
  } else {
    container.insertAdjacentHTML("beforeend", sideHTML(showEngineInfo, showMoves));
  }

  let resolveReady;
  const ready = new Promise((r) => { resolveReady = r; });

  const ctx = {
    onMoveJump, onPlayMoveClick, forkInfoFn, onForkClick,
    interactive, showClocks, showMoves, showEngineInfo,
    ready, resolveReady,

    currentFen: INITIAL_FEN,
    humanWhite: true,
    gameId: null,
    engineName: "Engine",
    playerName: PLAYER_NAME_DEFAULT,
    lastTurn: SIDE.WHITE,
    lastClockRunning: false,
    viewing: false,
    editing: false,
    // First board_update after (re)mount: snap pieces to position instead
    // of animating from startpos, and resolve `ready` so the router can
    // reveal the perspective without a render-order flicker.
    firstBoardUpdate: true,
    // Edit-mode side-to-move ("w"|"b"). Authoritative while editing.
    editStm: FEN_STM.WHITE,
    // Analysis mode: streams PV from a dedicated engine even while viewing.
    analyzing: false,
    // Cached PGN names so flipping the board in view mode can re-swap
    // top/bottom without waiting for a fresh board_update.
    viewWhiteName: null,
    viewBlackName: null,
    // Preview overlay: while previewActive the board shows a hypothetical
    // FEN (typically an AI `analyze` arg) and input is suppressed.
    previewActive: false,
    previewInputWasEnabled: false,
    // Piece-rendering hold during an optimistic move's stop/resume window;
    // see holdBoard/releaseBoard.
    boardHold: false,
    holdPending: null,
    holdBaseFen: null,
    scheduleRecompute: null,
    off: null,
  };

  queryRefs(ctx, container, sideHost);
  if (ctx.moveListEl) {
    markSelectable(ctx.moveListEl, { rows: ".move-row" });
  }
  if (ctx.fenText) ctx.fenText.textContent = INITIAL_FEN;
  ctx.fenCopyBtn?.addEventListener("click", () => copyFen(ctx));
  ctx.fenText?.addEventListener("click", () => copyFen(ctx));

  ctx.board = mountBoard({
    element: ctx.boardEl,
    styleId: boardStyle,
    onMove: (uci) => { if (interactive) onMove?.(uci); },
  });

  // cm-chessboard sizes its SVG off boardEl.clientWidth (squared), ignoring
  // height. We compute a square that fits the column width AND the viewport
  // height, then drive cm-chessboard's measurement.
  ctx.boardCol = container.querySelector(".game-view-board") || container;

  setHumanWhite(ctx, ctx.humanWhite);

  ctx.onRecompute = () => recomputeBoardSize(ctx);
  ctx.ro = new ResizeObserver(ctx.onRecompute);
  ctx.ro.observe(ctx.boardCol);
  ctx.ro.observe(document.body);
  window.addEventListener("resize", ctx.onRecompute);
  window.addEventListener(APP_EVT.LAYOUT_CHANGED, ctx.onRecompute);
  requestAnimationFrame(ctx.onRecompute);

  ctx.onVisibilityChange = () => { if (!document.hidden) ctx.board.cancelAnimations(); };
  ctx.onWindowFocus = () => ctx.board.cancelAnimations();
  document.addEventListener("visibilitychange", ctx.onVisibilityChange);
  window.addEventListener("focus", ctx.onWindowFocus);

  if (events) ctx.off = events.on((evt) => applyEvent(ctx, evt));

  return buildViewApi(ctx);
}
