// Tournament workspace: WinBox windows for Standings, Schedule, Event log,
// plus on-demand Live Game windows (one per engine POV).
//
// State: one workspace per tab; opening a different tournament closes the
// prior one. Desktop state (open windows, position, size, min/max, z-order)
// is snapshotted at the explicit save points (close / closeAll / finalize)
// and restored on re-open. WinBox events are not monitored continuously.
//
// Data flow:
//   GET /api/tournaments/{id} on open -> seed standings + schedule.
//   WS `tournament_status`             -> refresh metadata + standings.
//   WS `tournament_update`             -> event log; refresh on game-finished.
//   Periodic GET while running         -> reconcile standings.

import {
  closeAllLiveGames, getLiveWindows, touchLiveWindow,
  isLiveWindowOpen, openLiveGameWindow, openFrozenGameWindow,
  LIVE_MIN_WIDTH, LIVE_MIN_HEIGHT, DEBUG_WATCH,
} from "./tournament-live-game.js";
import { EVT, EVT_PREFIX, KIND, STATUS, SPRT } from "./tournament-events.js";
import { SIDE } from "./chess-consts.js";
import { APP_EVT } from "./app-events.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadJson, saveJson, removeKey } from "./storage.js";
import { CONFIRM_WIPE_QS, buildRestartConfirm } from "./tournament-restart.js";
import { attachColumnResize } from "./col-resize.js";
import { apiErrorDetail, confirm, toast } from "./dialogs.js";
import {
  AUTOSCROLL_SLACK_ROW_PX,
  cssVarPx,
  escapeHtml,
  flashWindow,
  isPinnedToBottom,
  scrollToBottom,
} from "./wb-utils.js";
import { createSlotGrid, SLOT_GAP } from "./workspace-slot-grid.js";

const STORAGE_KEY_PREFIX = STORAGE_KEY.WORKSPACE_PREFIX;
const STANDINGS_COL_PCTS_KEY = STORAGE_KEY.TOURNAMENTS_STANDINGS_COL_PCTS;
const STANDINGS_DEFAULT_PCTS = [25, 7, 7, 7, 7, 7, 8, 14];
const EVENT_LOG_LIMIT = 500;

// Reserved strip at the bottom so minimized WinBoxes have a place to dock.
// Source of truth is the --wb-min-footer-h CSS token (toast stacks lift
// above the same strip); read lazily since the stylesheet may not be
// applied yet at module-eval time.
const MINIMIZE_FOOTER_FALLBACK = 36;
function minimizeFooterH() {
  return cssVarPx("--wb-min-footer-h", MINIMIZE_FOOTER_FALLBACK);
}
// Visual gap between tiled/snapped windows; also absorbs WinBox rounding.
const TILE_MARGIN = 0;
const TIDY_GAP = 0;

// ---- Window body builders (pure DOM; no workspace state) ----------------

function makeStandingsBody() {
  const el = document.createElement("div");
  el.className = "wb-standings";
  el.innerHTML = `
    <div class="wb-sprt-slot"></div>
    <div class="wb-partial-slot"></div>
    <div class="wb-empty wb-standings-empty">Loading...</div>
    <div class="wb-standings-table-wrap" hidden>
      <table class="wb-table wb-standings-tbl">
        <colgroup>
          <col><col><col><col><col><col><col><col>
        </colgroup>
        <thead>
          <tr>
            <th>Engine<span class="th-grip"></span></th>
            <th>G<span class="th-grip"></span></th>
            <th>W<span class="th-grip"></span></th>
            <th>L<span class="th-grip"></span></th>
            <th>D<span class="th-grip"></span></th>
            <th>Pts<span class="th-grip"></span></th>
            <th>%<span class="th-grip"></span></th>
            <th>Elo<span class="th-grip"></span></th>
            <th>Ordo</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  `;
  const wrapEl = el.querySelector(".wb-standings-table-wrap");
  const tableEl = el.querySelector(".wb-standings-tbl");
  const colEls = Array.from(el.querySelectorAll(".wb-standings-tbl col"));
  const grips = Array.from(el.querySelectorAll(".wb-standings-tbl .th-grip"));
  const colPcts = STANDINGS_DEFAULT_PCTS.slice();
  const minPct = 4;
  attachColumnResize({
    table: tableEl,
    grips,
    overlayHost: wrapEl,
    storageKey: STANDINGS_COL_PCTS_KEY,
    sizes: colPcts,
    unit: "pct",
    applySizes(sizes, rctx) {
      if (rctx) {
        const { deltaFrac, startSizes, gripIdx } = rctx;
        const dPct = deltaFrac * 100;
        let a = startSizes[gripIdx] + dPct;
        let b = startSizes[gripIdx + 1] - dPct;
        if (a < minPct) { b -= minPct - a; a = minPct; }
        if (b < minPct) { a -= minPct - b; b = minPct; }
        sizes[gripIdx] = a;
        sizes[gripIdx + 1] = b;
      }
      colEls.forEach((c, i) => { c.style.width = sizes[i] + "%"; });
    },
  });
  return el;
}

function makeScheduleBody() {
  const el = document.createElement("div");
  el.className = "wb-schedule";
  el.innerHTML = `<div class="wb-empty">Loading...</div>`;
  return el;
}

function makeLogBody() {
  const el = document.createElement("div");
  el.className = "wb-eventlog";
  el.innerHTML = `<div class="wb-error-banner" hidden></div><ul class="wb-eventlog-list"></ul>`;
  return el;
}

function makeEnginesBody() {
  const el = document.createElement("div");
  el.className = "wb-engines";
  el.innerHTML = `<div class="wb-empty">Loading...</div>`;
  return el;
}


// ---- Geometry + snapshot (read-only over ctx collections) ---------------

// Geometry as CSS px-strings ("123px") for localStorage snapshots and
// WinBox config. Distinct from play-dock-windows.js's wbGeometry, which
// returns raw numbers -- the two are NOT interchangeable.
function wbGeometryPx(wb) {
  return {
    x: `${Math.round(wb.x)}px`,
    y: `${Math.round(wb.y)}px`,
    width: `${Math.round(wb.width)}px`,
    height: `${Math.round(wb.height)}px`,
  };
}

function snapshotLive(ctx) {
  return getLiveWindows()
    .filter(wb => wb._watchOpts)
    .map(wb => {
      const resolved = wb._watchOpts.gameId ? ctx.resolvedGames.get(wb._watchOpts.gameId) : null;
      return {
        ...wb._watchOpts,
        ...wbGeometryPx(wb),
        min: !!wb.min, max: !!wb.max, z: wb.index ?? 0,
        ...(resolved ? { resolved } : {}),
      };
    });
}

function snapshot(ctx) {
  const state = {};
  for (const key of Object.keys(ctx.windows)) {
    const wb = ctx.windows[key];
    state[key] = wb
      ? { open: true, ...wbGeometryPx(wb), min: !!wb.min, max: !!wb.max, z: wb.index ?? 0 }
      : { open: false, ...ctx.lastGeometry[key], min: false, max: false, z: 0 };
  }
  state.live = snapshotLive(ctx);
  state._layout = ctx.activeLayout;
  return state;
}


// Min sizes scale with the root font size. 280px / 320px / 120px at
// default 16px match the previous hardcoded values; em keeps the
// proportions intact when the user changes font size.
function getMinSizes() {
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
  const wWide = Math.round(rem * 20);    // 320px at 16px
  const wNarrow = Math.round(rem * 17.5); // 280px at 16px
  const h = Math.round(rem * 7.5);        // 120px at 16px
  return {
    standings: { minwidth: wWide,   minheight: h },
    schedule:  { minwidth: wWide,   minheight: h },
    engines:   { minwidth: wNarrow, minheight: h },
    log:       { minwidth: wNarrow, minheight: h },
  };
}

function setShadow(wb, on) {
  wb.g?.classList.toggle("no-shadow", !on);
}

// Send a window to the minimize footer: minimize + shadow on.
function minimizeToDock(wb) {
  try { wb.minimize(); } catch { /* */ }
  setShadow(wb, true);
}

function openWindows(ctx) {
  return [...Object.values(ctx.windows).filter(Boolean), ...getLiveWindows()];
}

function unminimize(wb) {
  // resize/move on a minimized or maximized WinBox leaves it stuck in
  // that state -- restore first so the new geometry actually takes.
  if (wb.min || wb.max) wb.restore();
}

// Focus windows top-left first so bottom-right ends up on top.
function zOrder(wbs) {
  [...wbs].sort((a, b) => a.y !== b.y ? a.y - b.y : a.x - b.x)
    .forEach(wb => { try { wb.focus(); } catch { /* */ } });
}

// wbsIn: explicit list (snap fallback -- skip minimized, don't unminimize).
function tile(ctx, wbsIn, { reserveDock = false, preserveMin = false, preserveMax = false } = {}) {
  setLayout(ctx, LAYOUT.TILE);
  const { left, top } = ctx;
  let wbs = wbsIn ?? openWindows(ctx);
  // From openWindows (not wbs): snap's fallback passes a pre-filtered list.
  const keepMaxed = preserveMax && openWindows(ctx).some(wb => wb.max);
  if (preserveMax) wbs = wbs.filter(wb => !wb.max);
  if (preserveMin) wbs = wbs.filter(wb => !wb.min);
  else wbs.forEach(unminimize);
  if (!wbs.length) return;
  const availW = ctx.getRight() - left;
  const availH = window.innerHeight - top - (reserveDock ? minimizeFooterH() : 0);
  const maxMinW = Math.max(...wbs.map(wb => wb.svMinWidth ?? 0));
  const maxMinH = Math.max(...wbs.map(wb => wb.svMinHeight ?? 0));
  const n = wbs.length;
  const maxCols = maxMinW ? Math.floor(availW / maxMinW) : n;
  const minCols = maxMinH ? Math.ceil(n / Math.max(1, Math.floor(availH / maxMinH))) : 1;
  const cols = Math.min(maxCols, Math.max(minCols, Math.ceil(Math.sqrt(n))));
  const rows = Math.ceil(n / cols);
  const w = Math.floor(availW / cols);
  const h = Math.floor(availH / rows);
  wbs.forEach((wb, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    // Window size = cell minus TILE_MARGIN, but floored to per-window min
    // (live-game windows stash svMinWidth/svMinHeight at creation; WinBox
    // doesn't expose config min* on the instance).
    const ww = Math.max(w - TILE_MARGIN, wb.svMinWidth ?? 0);
    const hh = Math.max(h - TILE_MARGIN, wb.svMinHeight ?? 0);
    // Clamp position so bottom-right stays inside [availW, availH] when
    // min size > cell size -- prevents the bottom row from spilling into
    // the reserved dock area. Trade-off: pushed windows may overlap the
    // row/column above them. Acceptable for this "didn't fit" fallback.
    const x = Math.max(left, Math.min(left + col * w, left + availW - ww));
    const y = Math.max(top,  Math.min(top  + row * h, top  + availH - hh));
    wb.resize(ww, hh).move(x, y);
    setShadow(wb, false);
  });
  // Don't re-stack while a maximized window is preserved on top.
  if (!keepMaxed) zOrder(wbs);
}

// 2x2 in the bottom half of the viewport. Auto-opens any of the
// four target windows that aren't open yet.
function tidy(ctx, { preserveMin = false } = {}) {
  setLayout(ctx, LAYOUT.TIDY);
  const { left, top, slotGrid } = ctx;
  const keys = ["engines", "standings", "schedule", "log"];
  for (const k of keys) {
    if (!ctx.windows[k]) openSystemWindow(ctx, k, { flash: false });
  }
  // If watchers exist, re-grid them first while the 4 system panels
  // are hidden, so the user doesn't see the panels flicker beneath
  // the watcher reshuffle. Overflow watchers are minimized.
  const watchers = getLiveWindows();
  if (watchers.length > 0) {
    for (const k of keys) {
      const wb = ctx.windows[k];
      if (wb) try { wb.hide(); } catch { /* */ }
    }
    const cap = slotGrid.capacity();
    let slot = 0;
    // Sort by current x position so slot assignment matches physical order,
    // not registration order. Minimized/maximized go last when preserving.
    const ordered = [...watchers].sort((a, b) => {
      const aDeferred = preserveMin && (a.min || a.max);
      const bDeferred = preserveMin && (b.min || b.max);
      if (aDeferred !== bDeferred) return aDeferred ? 1 : -1;
      return a.x !== b.x ? a.x - b.x : a.y - b.y;
    });
    ordered.forEach(wb => {
      if (preserveMin && (wb.min || wb.max)) {
        return; // onReflow handles slot placement on restore
      }
      if (slot < cap) {
        unminimize(wb);
        const r = slotGrid.rectAt(slot++);
        wb.resize(r.w, r.h).move(r.x, r.y);
        setShadow(wb, false);
      } else {
        minimizeToDock(wb);
      }
    });
    for (const k of keys) {
      const wb = ctx.windows[k];
      if (wb) try { wb.show(); } catch { /* */ }
    }
  }
  const availW = ctx.getRight() - left;
  const availH = window.innerHeight - top - minimizeFooterH();
  const mins = getMinSizes();
  const leftW = Math.max(Math.round(availW * 0.35), mins.engines.minwidth);
  const rightW = availW - leftW - TIDY_GAP;
  // System rows get what's left after one row of board slots.
  // Clamp each row so both windows in a row share the same height
  // (WinBox silently floors to per-window minheight otherwise).
  const systemH = availH - LIVE_MIN_HEIGHT();
  const desiredRowH = Math.floor((systemH - TIDY_GAP) / 2);
  const topRowH = Math.max(desiredRowH, mins.engines.minheight, mins.standings.minheight);
  const botRowH = Math.max(desiredRowH, mins.schedule.minheight, mins.log.minheight);
  // Anchor bottom edge to top + availH (which already excludes the
  // minimize footer). If clamped rows exceed availH the layout
  // extends upward, but never below the reserved footer.
  const regionTop = top + availH - (topRowH + TIDY_GAP + botRowH);
  const placements = [
    ["engines",   left,                   regionTop,                       leftW,  topRowH],
    ["standings", left + leftW + TIDY_GAP, regionTop,                      rightW, topRowH],
    ["schedule",  left,                   regionTop + topRowH + TIDY_GAP,  leftW,  botRowH],
    ["log",       left + leftW + TIDY_GAP, regionTop + topRowH + TIDY_GAP, rightW, botRowH],
  ];
  for (const [k, x, y, w, h] of placements) {
    const wb = ctx.windows[k];
    if (!wb) continue;
    unminimize(wb);
    wb.resize(w, h).move(x, y);
    setShadow(wb, false);
  }
  if (!preserveMin) zOrder(openWindows(ctx).filter(wb => !wb.min));
}

// Snap: k-d tree / slice-and-dice partition. Recursively split the
// viewport at the axis of greatest center-spread; each leaf gets one
// window. Produces a perfect rectangular tiling -- no gaps, no overlaps,
// O(N log N), idempotent. Minimized/maximized windows are skipped.
function snap(ctx, { preserveMax = false } = {}) {
  setLayout(ctx, LAYOUT.SNAP);
  const { left, top } = ctx;
  const vx0 = left, vy0 = top;
  const vx1 = ctx.getRight();

  const allWindows = openWindows(ctx);
  // Restore any maximized windows so they participate in the snap layout
  // (otherwise non-max windows would be tiled invisibly underneath them)
  // -- unless preserved, in which case they stay maximized on top and
  // the survivors snap around them. Minimized windows stay excluded.
  if (!preserveMax) for (const wb of allWindows) if (wb.max) wb.restore();
  const keepMaxed = preserveMax && allWindows.some(wb => wb.max);
  const wbs = allWindows.filter(wb => !wb.min && !wb.max);
  if (!wbs.length) return;
  // Reserve bottom strip for the minimize dock only if any window is
  // currently minimized -- otherwise full viewport.
  const hasMin = allWindows.some(wb => wb.min);
  const vy1 = window.innerHeight - (hasMin ? minimizeFooterH() : 0);

  const items = wbs.map(wb => ({
    wb,
    cx: wb.x + wb.width / 2,
    cy: wb.y + wb.height / 2,
    cw: wb.width,
    ch: wb.height,
    minW: wb.svMinWidth ?? 1,
    minH: wb.svMinHeight ?? 1,
    rect: null,
  }));

  function partition(rect, group) {
    if (group.length === 1) { group[0].rect = rect; return; }
    let xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
    for (const it of group) {
      if (it.cx < xMin) xMin = it.cx;
      if (it.cx > xMax) xMax = it.cx;
      if (it.cy < yMin) yMin = it.cy;
      if (it.cy > yMax) yMax = it.cy;
    }
    const xSpread = xMax - xMin, ySpread = yMax - yMin;
    const cutV = xSpread > ySpread || (xSpread === ySpread && rect.w >= rect.h);
    group.sort((a, b) => cutV ? a.cx - b.cx : a.cy - b.cy);
    const mid = Math.floor(group.length / 2);
    const A = group.slice(0, mid), B = group.slice(mid);
    // Cut between the rightmost (bottommost) edge of A and the leftmost
    // (topmost) edge of B. Edge-based cut is idempotent under TILE_MARGIN:
    // after snap, A's max edge = cut - TILE_MARGIN, B's min edge = cut,
    // and round((cut - 1 + cut) / 2) = cut, so subsequent snaps don't drift.
    if (cutV) {
      let aMax = -Infinity, bMin = Infinity;
      for (const it of A) { const e = it.cx + it.cw / 2; if (e > aMax) aMax = e; }
      for (const it of B) { const e = it.cx - it.cw / 2; if (e < bMin) bMin = e; }
      const cut = Math.round((aMax + bMin) / 2);
      const c = Math.max(rect.x + 1, Math.min(cut, rect.x + rect.w - 1));
      partition({ x: rect.x, y: rect.y, w: c - rect.x, h: rect.h }, A);
      partition({ x: c, y: rect.y, w: rect.x + rect.w - c, h: rect.h }, B);
    } else {
      let aMax = -Infinity, bMin = Infinity;
      for (const it of A) { const e = it.cy + it.ch / 2; if (e > aMax) aMax = e; }
      for (const it of B) { const e = it.cy - it.ch / 2; if (e < bMin) bMin = e; }
      const cut = Math.round((aMax + bMin) / 2);
      const c = Math.max(rect.y + 1, Math.min(cut, rect.y + rect.h - 1));
      partition({ x: rect.x, y: rect.y, w: rect.w, h: c - rect.y }, A);
      partition({ x: rect.x, y: c, w: rect.w, h: rect.y + rect.h - c }, B);
    }
  }

  partition({ x: vx0, y: vy0, w: vx1 - vx0, h: vy1 - vy0 }, items);

  // If any leaf rect can't accommodate the window's min size, fall back to
  // tile. Always reserve the dock here: this is the "didn't fit" path and
  // a window may well end up minimized as part of recovery.
  for (const it of items) {
    if (it.rect.w - TILE_MARGIN < it.minW || it.rect.h - TILE_MARGIN < it.minH) {
      tile(ctx, wbs, { reserveDock: true, preserveMax });
      setLayout(ctx, LAYOUT.SNAP);  // restore -- tile() above overwrites it
      return;
    }
  }

  for (const it of items) {
    const r = it.rect;
    it.wb.resize(r.w - TILE_MARGIN, r.h - TILE_MARGIN).move(r.x, r.y);
    setShadow(it.wb, false);
  }
  // Don't re-stack while a maximized window is preserved on top.
  if (!keepMaxed) zOrder(wbs);
}

function untidy(ctx) {
  setLayout(ctx, LAYOUT.NONE);
  for (const wb of openWindows(ctx)) setShadow(wb, true);
}

function reapplyLayout(ctx) {
  // Bail once torn down: a stray rAF/timer scheduled before close must not
  // re-enter tidy(), which would re-open the just-closed panels (the X-close
  // -> windows hit 0 -> resurrected-to-4 race).
  if (ctx.finalized) return;
  if (ctx.activeLayout === LAYOUT.TIDY) tidy(ctx, { preserveMin: true });
  else if (ctx.activeLayout === LAYOUT.TILE) tile(ctx, null, { preserveMin: true, preserveMax: true, reserveDock: true });
  else if (ctx.activeLayout === LAYOUT.SNAP) snap(ctx, { preserveMax: true });
}

function minimizeAll(ctx) {
  const wbs = openWindows(ctx).filter(wb => !wb.min);
  for (const wb of wbs) try { wb.minimize(); } catch { /* */ }
  return wbs;
}

function restoreWindows(wbs) {
  for (const wb of wbs) try { unminimize(wb); } catch { /* */ }
}


// ---- Window construction + reflow/header wiring -------------------------

// Minimize the oldest watcher that actually holds a slot (insertion order =
// open order) so a restored window can take its cell. Skips `keep`, minimized,
// maximized, and dragged-out windows whose rect no longer covers any slot.
// Returns the evicted window, or null if none qualifies.
function evictOldestSlotted(ctx, keep) {
  for (const wb of getLiveWindows()) {
    if (wb === keep || wb.min || wb.max) continue;
    if (!ctx.slotGrid.occupiesSlot(wb)) continue;
    minimizeToDock(wb);
    return wb;
  }
  return null;
}

// Shared onminimize/onrestore handler for all windows. Reads ctx.activeLayout
// at call time so layout switches never leave stale handlers.
function onReflow(ctx, wb, isRestore) {
  if (isRestore && ctx.pendingDragX !== null) {
    // Drag-unmaximize: place restored window so cursor stays at the same
    // proportional position on the title bar it occupied when maximized.
    const x0 = ctx.pendingDragX;
    const ratio = Math.max(0, Math.min(1, (x0 - ctx.left) / (window.innerWidth - ctx.left)));
    const x = Math.max(ctx.left, Math.min(x0 - Math.round(ratio * wb.width), window.innerWidth - wb.width));
    wb.move(x, wb.y);
    ctx.pendingDragX = null;
    return;
  }
  if (ctx.activeLayout === LAYOUT.TIDY) {
    // slotGrid only positions live (watcher) windows. System windows
    // belong to the TIDY region grid -- reflow the whole layout.
    if (isRestore) {
      if (!getLiveWindows().includes(wb)) {
        requestAnimationFrame(() => reapplyLayout(ctx));
        return;
      }
      let c = ctx.slotGrid.claim(wb);
      if (!c) {
        // Grid is full: evict the oldest slotted watcher to the dock so the
        // freshly-clicked window cycles in. Without this the restore would
        // re-minimize and nothing visible would happen.
        const evicted = evictOldestSlotted(ctx, wb);
        if (evicted) c = ctx.slotGrid.claim(wb);
      }
      if (c) {
        wb._justRestored = true;
        touchLiveWindow(wb);
        wb.resize(c.w, c.h).move(c.x, c.y);
        setShadow(wb, false);
      } else {
        // No slot even after eviction: the grid shrank (e.g. browser resized
        // while this window was maximized) so stale slot positions no longer
        // fit. Re-tidy the whole set, re-gridding survivors.
        requestAnimationFrame(() => tidy(ctx, { preserveMin: true }));
      }
    }
  } else if (ctx.activeLayout === LAYOUT.TILE || ctx.activeLayout === LAYOUT.SNAP) {
    requestAnimationFrame(() => reapplyLayout(ctx));
  }
}

// Reflow + header wiring so any workspace window (system, live, frozen)
// participates in TIDY/TILE/SNAP layouts and slot-grid placement.
function wireLayoutHandlers(ctx, wb) {
  wb.onminimize = () => onReflow(ctx, wb, false);
  wb.onrestore = () => onReflow(ctx, wb, true);
  wireHeader(ctx, wb);
  // Initial shadow/corner state follows the layout: free-floating (NONE)
  // means shadow + rounded; managed layouts re-assert no-shadow per reflow.
  setShadow(wb, ctx.activeLayout === LAYOUT.NONE);
}

// Wire the drag-unmaximize repositioning on a window's header.
function wireHeader(ctx, wb) {
  const dragEl = wb.g?.querySelector(".wb-drag");
  if (!dragEl) return;
  dragEl.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (wb.max) {
      // Arm on first mousemove rather than mousedown: if WinBox restores
      // via dblclick the restore fires before the 2nd mousedown, so we
      // can't tell on mousedown alone whether a drag or dblclick follows.
      wb._armX = e.pageX;
      const onMove = () => {
        if (wb._armX !== null) {
          ctx.pendingDragX = wb._armX;
          wb._armX = null;
        }
      };
      window.addEventListener("mousemove", onMove, { once: true });
      window.addEventListener("mouseup", () => {
        window.removeEventListener("mousemove", onMove);
        wb._armX = null;
        ctx.pendingDragX = null;
      }, { once: true });
    } else if (!wb._justRestored) {
      // Any header mousedown shows shadow; reapplyLayout clears it.
      setShadow(wb, true);
    }
    wb._justRestored = false;
  }, { capture: true });
}

function makeBox(ctx, key, title, body, { min = false, max = false } = {}) {
  const cfg = ctx.lastGeometry[key];
  const extraClass = ctx.windowSpecs?.[key]?.extraClass;
  const extra = extraClass ? ` ${extraClass}` : "";
  const sizes = getMinSizes()[key];
  const wb = new WinBox({
    title, mount: body, top: ctx.top, left: ctx.left, right: ctx.getRightInset(), min, max,
    ...(cfg ? { x: cfg.x, y: cfg.y, width: cfg.width, height: cfg.height } : {}),
    class: `sturddle-wb no-full${extra}`,
    ...sizes,
  });
  // Stash so tile()/snap() can read the effective min size from the
  // instance (WinBox doesn't expose its config min* on the instance).
  wb.svMinWidth = sizes.minwidth;
  wb.svMinHeight = sizes.minheight;
  // Wire onclose after construction (TDZ on `wb` otherwise). No persist
  // here -- state is captured at workspace.close()/closeAll()/finalize().
  wb.onclose = () => {
    ctx.lastGeometry[key] = wbGeometryPx(wb);
    ctx.windows[key] = null;
    if (Object.values(ctx.windows).every((w) => w === null)) tearDown(ctx);
    else if (ctx.activeLayout !== LAYOUT.TIDY) requestAnimationFrame(() => reapplyLayout(ctx));
    return false;
  };
  wireLayoutHandlers(ctx, wb);
  if (ctx.top > 0 && wb.y < ctx.top) wb.move(wb.x, ctx.top);
  if (ctx.left > 0 && wb.x < ctx.left) wb.move(ctx.left, wb.y);
  return wb;
}


// ---- Live-game watch ----------------------------------------------------

function attachWatch(ctx, btn, attachKey, openOpts) {
  if (DEBUG_WATCH) console.log("[WATCH] click", { attachKey, openOpts });
  // Slot grid is tidy-mode only; tile/snap reflow handles placement.
  const useSlotsGrid = ctx.activeLayout === LAYOUT.TIDY;
  // Claim a slot BEFORE creating the window so the new window's own
  // default position doesn't shadow the slot it would occupy.
  // Skip claim for windows restored minimized/maximized -- they don't slot.
  const rawClaim = (useSlotsGrid && !openOpts.min && !openOpts.max && !isLiveWindowOpen(attachKey)) ? ctx.slotGrid.claim() : null;
  // Clamp to viewport so a slot near the right/bottom edge can't
  // push the window off-screen.
  const claim = rawClaim ? {
    ...rawClaim,
    x: Math.min(rawClaim.x, Math.max(ctx.left, window.innerWidth - rawClaim.w)),
    y: Math.min(rawClaim.y, Math.max(ctx.top, window.innerHeight - rawClaim.h)),
  } : null;
  let result;
  try {
    result = openLiveGameWindow({
      ...openOpts, token: ctx.token, tournamentId: ctx.tournament.id,
      top: ctx.top, left: ctx.left, right: ctx.getRightInset(),
      boardStyle: ctx.boardStyleCached,
      initialRect: claim ? { x: claim.x, y: claim.y, w: claim.w, h: claim.h } : (openOpts.initialRect ?? null),
    });
  } catch (e) {
    console.error("[WATCH] openLiveGameWindow threw", e, { attachKey, openOpts });
    return;
  }
  // No slot fit in tidy/none mode -- minimize so the grid stays clean.
  if (result?.wb && !result.alreadyOpen && useSlotsGrid && !claim && !result.wb.max) {
    try { result.wb.minimize(); } catch { /* */ }
  }
  if (result?.wb && !result.alreadyOpen && !result.wb.min && !result.wb.max) requestAnimationFrame(() => reapplyLayout(ctx));
  if (result?.wb && !result.alreadyOpen) wireLayoutHandlers(ctx, result.wb);
  const isLive = isLiveWindowOpen(attachKey);
  if (DEBUG_WATCH) console.log("[WATCH] post-open", { attachKey, isLive, slotted: !!claim });
  btn?.classList.toggle("wb-sched-attach-btn--live", isLive);
}

function refreshWatchButtons(ctx) {
  for (const body of [ctx.scheduleBody, ctx.enginesBody]) {
    for (const btn of body.querySelectorAll(".wb-sched-attach-btn")) {
      btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(btn.title));
    }
  }
}


// ---- Data refresh -------------------------------------------------------

async function refresh(ctx) {
  let fresh;
  try {
    fresh = await ctx.api("GET", `/api/tournaments/${ctx.tournament.id}`);
  } catch (e) {
    ctx.log?.(`workspace refresh failed: ${e.message}`);
    return;
  }
  applyDetail(ctx, fresh);
}

// Tracks any OTHER tournament currently running. Drives the failure
// banner's Restart disabled state -- restarting while another holds
// the single-active slot would 409 after a confirmed wipe.
function setOtherActive(ctx, id, name) {
  const nextId = (id && id !== ctx.tournament.id) ? id : null;
  const nextName = nextId ? (name || null) : null;
  if (nextId === ctx.otherActiveId && nextName === ctx.otherActiveName) return;
  ctx.otherActiveId = nextId;
  ctx.otherActiveName = nextName;
  renderEventLog(ctx);
}

function applyDetail(ctx, fresh) {
  ctx.detail = fresh;
  // While RUNNING, API state can lag WS events under fast tc, so
  // treat API as additive (add missing entries, never remove).
  // When not RUNNING, replace authoritatively to drop ghosts.
  const seededProxies = ctx.detail.proxies_active || [];
  if (ctx.detail.status !== STATUS.RUNNING) ctx.activeProxies.clear();
  for (const p of seededProxies) {
    if (p.proxy_id && !ctx.activeProxies.has(p.proxy_id)) {
      ctx.activeProxies.set(p.proxy_id, { engineName: p.engine_name || null });
    }
  }
  const seededPairings = ctx.detail.pairings_active || [];
  if (ctx.detail.status !== STATUS.RUNNING) ctx.livePairings.clear();
  for (const p of seededPairings) {
    if (ctx.livePairings.has(p.proxy_a) || ctx.livePairings.has(p.proxy_b)) continue;
    const info = { pairId: p.pair_id, proxyA: p.proxy_a, engineA: p.engine_a, sideA: p.side_a,
                   proxyB: p.proxy_b, engineB: p.engine_b, sideB: p.side_b };
    ctx.livePairings.set(p.proxy_a, info);
    ctx.livePairings.set(p.proxy_b, info);
  }
  renderStandings(ctx);
  renderSchedule(ctx);
  renderEngines(ctx);
  renderEventLog(ctx);
}


// ---- Event pipeline -----------------------------------------------------

function addLogEntry(ctx, evt) {
  const seq = evt.payload?._seq;
  // `game_reconciled` doesn't go into eventLog (it upgrades a prior
  // game_finished row in pushEvent); duplicates are idempotent there
  // so we don't need to track its seq at all.
  if (evt.payload?.kind === KIND.GAME_RECONCILED) return false;
  if (seq != null) {
    if (ctx.seenSeqs.has(seq)) return false;
    ctx.seenSeqs.add(seq);
  }
  const tsRaw = evt.payload?._ts;
  const ts = (tsRaw ? new Date(tsRaw) : new Date())
    .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  ctx.eventLog.push({ ts, kind: evt.kind, payload: evt.payload, _seq: seq });
  // Keep ordered by seq so backfill items slot in before any live
  // events that arrived during the REST round-trip.
  ctx.eventLog.sort((a, b) => (a._seq ?? 0) - (b._seq ?? 0));
  // Cap eventLog and keep seenSeqs in lockstep so it can't outgrow
  // the visible log -- the dedup only needs to cover items we'd
  // otherwise re-render.
  while (ctx.eventLog.length > EVENT_LOG_LIMIT) {
    const evicted = ctx.eventLog.shift();
    if (evicted?._seq != null) ctx.seenSeqs.delete(evicted._seq);
  }
  return true;
}

function applyReconciled(ctx, evt) {
  // Upgrade the prior `game_finished` entry for this pair_id with
  // the matched result/termination/game_n instead of pushing a
  // separate row. One game = one log entry.
  const pid = evt.payload?.pair_id;
  if (pid) {
    if (evt.payload.game_n != null) {
      ctx.resolvedGames.set(pid, {
        gameN: evt.payload.game_n,
        result: evt.payload.result,
        termination: evt.payload.termination,
      });
    }
    for (let i = ctx.eventLog.length - 1; i >= 0; i--) {
      const ent = ctx.eventLog[i];
      if (ent.payload?.kind === KIND.GAME_FINISHED && ent.payload?.pair_id === pid) {
        ent.payload = {
          ...ent.payload,
          result: evt.payload.result,
          termination: evt.payload.termination,
          game_n: evt.payload.game_n,
          reconciled: true,
        };
        break;
      }
    }
    // Live windows listening for this pair_id will repaint
    // their banner; no-op if window already closed.
    window.dispatchEvent(new CustomEvent(APP_EVT.RECONCILED, {
      detail: {
        pairId: pid,
        result: evt.payload.result,
        termination: evt.payload.termination,
        gameN: evt.payload.game_n ?? null,
        tournamentId: ctx.tournament.id,
      },
    }));
  }
}

function applyEventKind(ctx, evt, inner) {
  // Track active proxies for Schedule rows.
  if (inner === KIND.PROXY_STARTED) {
    const pid = evt.payload.proxy_id;
    if (pid) {
      ctx.activeProxies.set(pid, {
        engineName: evt.payload.engine_name || null,
      });
    }
  } else if (inner === KIND.PROXY_ENDED) {
    const pid = evt.payload.proxy_id;
    if (pid) ctx.activeProxies.delete(pid);
  } else if (inner === KIND.PROXY_PAIRED) {
    const p = evt.payload;
    const info = { pairId: p.pair_id, proxyA: p.proxy_a, engineA: p.engine_a, sideA: p.side_a,
                   proxyB: p.proxy_b, engineB: p.engine_b, sideB: p.side_b };
    ctx.livePairings.set(p.proxy_a, info);
    ctx.livePairings.set(p.proxy_b, info);
  } else if (inner === KIND.GAME_FINISHED) {
    // Authoritative game-end signal -- drives livePairings cleanup +
    // Schedule re-render.
    ctx.livePairings.delete(evt.payload.proxy_a);
    ctx.livePairings.delete(evt.payload.proxy_b);
  } else if (inner === KIND.GAME_RECONCILED) {
    applyReconciled(ctx, evt);
  } else if (
    inner === KIND.DONE || inner === KIND.STOPPED ||
    (evt.kind === EVT.STATUS &&
     [STATUS.STOPPED, STATUS.DONE, STATUS.FAILED].includes(evt.payload?.status))
  ) {
    ctx.activeProxies.clear();
  }
}

function pushEvent(ctx, evt) {
  if (!evt) return;
  if (!evt.kind?.startsWith(EVT_PREFIX)) return;
  // Only events for *our* tournament -- the orchestrator stamps
  // tournament_id into payloads on the server side.
  const tid = evt.payload?.tournament_id;
  if (tid && tid !== ctx.tournament.id) return;

  const added = addLogEntry(ctx, evt);

  const inner = evt.payload?.kind;
  applyEventKind(ctx, evt, inner);

  if (added) scheduleRender(ctx, "_eventLogPending", renderEventLog);
  if (inner === KIND.PROXY_STARTED || inner === KIND.PROXY_ENDED ||
      inner === KIND.PROXY_PAIRED || inner === KIND.PROXY_UNPAIRED ||
      inner === KIND.GAME_FINISHED || evt.kind === EVT.STATUS ||
      inner === KIND.DONE || inner === KIND.STOPPED)
    scheduleRender(ctx, "_schedulePending", renderSchedule);
  if (inner === KIND.PROXY_STARTED || inner === KIND.PROXY_ENDED ||
      evt.kind === EVT.STATUS || inner === KIND.DONE || inner === KIND.STOPPED)
    scheduleRender(ctx, "_enginesPending", renderEngines);

  // Status changes and game finishes are good triggers to refresh
  // standings authoritatively.
  if (
    evt.kind === EVT.STATUS ||
    inner === KIND.GAME_FINISHED ||
    inner === KIND.DONE ||
    inner === KIND.STOPPED
  ) {
    refresh(ctx);
  }

  if (
    evt.kind === EVT.STATUS &&
    [STATUS.STOPPED, STATUS.FAILED].includes(evt.payload?.status)
  ) {
    closeAllLiveGames();
    if (evt.payload.status === STATUS.STOPPED) {
      toast(`"${ctx.tournament.name}" stopped`, { variant: "warning" });
    }
  }
  // Tournament started: auto-open Live Games so the user sees
  // pairings as they form. Skip if restoring a saved desktop state --
  // the user may have intentionally closed that window.
  if (
    !ctx.restoreFromSaved &&
    evt.kind === EVT.STATUS &&
    evt.payload?.status === STATUS.RUNNING &&
    ctx.windows.schedule == null
  ) {
    openSystemWindow(ctx, "schedule");
  }
}

function armSubscriptions(ctx) {
  if (ctx.unsubscribe == null) {
    ctx.unsubscribe = ctx.events.on((evt) => pushEvent(ctx, evt));
  }
}

async function backfillEvents(ctx) {
  try {
    const res = await ctx.api("GET", `/api/tournaments/${ctx.tournament.id}/events`);
    let added = false;
    for (const e of (res.events || [])) {
      if (!e.kind?.startsWith(EVT_PREFIX)) continue;
      if (addLogEntry(ctx, e)) added = true;
    }
    if (added) scheduleRender(ctx, "_eventLogPending", renderEventLog);
  } catch (e) {
    ctx.log?.(`event backfill failed: ${e.message}`);
  }
}

// ---- Lifecycle / listeners --------------------------------------------

// Periodic refresh is driven by the tournaments-list poll (single source);
// it calls refresh(ctx) on this workspace handle. Catches WS gaps + PGN-only
// changes (games_played advancing without the tailer subscribed).
function onReconnect(ctx, e) {
  if (!e.detail?.connected) {
    ctx.seenSeqs.clear();
    ctx.eventLog.length = 0;
    return;
  }
  refresh(ctx);
  backfillEvents(ctx);
}

function onBeforeUnload(ctx) {
  saveState(ctx.tournament.id, snapshot(ctx));
}

function onLiveGameClosedReapply(ctx) {
  if (ctx.activeLayout !== LAYOUT.TIDY) requestAnimationFrame(() => reapplyLayout(ctx));
}

// NONE layout: keep free floats reachable after a viewport shrink --
// clamp into the workspace area, shrinking only when a window exceeds
// it. WinBox enforces per-window min sizes on resize.
function clampFloats(ctx) {
  const right = ctx.getRight();
  const bottom = window.innerHeight;
  for (const wb of openWindows(ctx)) {
    if (wb.min || wb.max) continue;
    const w = Math.min(wb.width, right - ctx.left);
    const h = Math.min(wb.height, bottom - ctx.top);
    const x = Math.max(ctx.left, Math.min(wb.x, right - w));
    const y = Math.max(ctx.top, Math.min(wb.y, bottom - h));
    if (w !== wb.width || h !== wb.height) wb.resize(w, h);
    if (x !== wb.x || y !== wb.y) wb.move(x, y);
  }
}

function onResize(ctx) {
  clearTimeout(ctx.resizeTimer);
  ctx.resizeTimer = setTimeout(() => {
    // Refit maximized windows to the new viewport. Suppress onrestore for
    // the cycle: it's not a user restore, and onReflow would claim/evict
    // slots against half-updated geometry (spurious minimize).
    for (const wb of openWindows(ctx)) if (wb.max) {
      const onRestore = wb.onrestore;
      wb.onrestore = null;
      try { wb.restore(); wb.maximize(); } finally { wb.onrestore = onRestore; }
    }
    // Managed layouts re-grid the survivors around preserved min/max
    // windows, keeping them inside the workspace area (ribbon/header
    // insets); free floats just get clamped back into it.
    if (ctx.activeLayout === LAYOUT.NONE) clampFloats(ctx);
    else reapplyLayout(ctx);
  }, 150);
}

function attachResizeListeners(ctx) {
  window.addEventListener("resize", ctx.onResize);
  document.addEventListener("fullscreenchange", ctx.onResize);
}

function detachResizeListeners(ctx) {
  window.removeEventListener("resize", ctx.onResize);
  document.removeEventListener("fullscreenchange", ctx.onResize);
  clearTimeout(ctx.resizeTimer);
}

function onLiveGameClosed(ctx) {
  const allStandardClosed = Object.values(ctx.windows).every((w) => w === null);
  if (allStandardClosed && getLiveWindows().length === 0) finalize(ctx);
}

function finalize(ctx) {
  if (ctx.finalized) return;
  ctx.finalized = true;
  window.removeEventListener("beforeunload", ctx.onBeforeUnload);
  window.removeEventListener(APP_EVT.CONNECTION, ctx.onReconnect);
  window.removeEventListener(APP_EVT.LIVEGAME_CLOSED, ctx.onRefreshWatchButtons);
  window.removeEventListener(APP_EVT.LIVEGAME_CLOSED, ctx.onLiveGameClosedReapply);
  detachResizeListeners(ctx);
  if (ctx.liveWatcherAttached) {
    window.removeEventListener(APP_EVT.LIVEGAME_CLOSED, ctx.onLiveGameClosed);
    ctx.liveWatcherAttached = false;
  }
  // User X-closed the last window: persist a dismissed snapshot so a
  // future navigation does not auto-reopen the workspace.
  if (!ctx.explicitlyClosed) {
    saveState(ctx.tournament.id, { ...snapshot(ctx), _closed: true });
  }
  // Clearing the active-workspace pointer makes getActiveLayout() report
  // NONE -- no separate layout reset needed now that layout is per-ctx.
  if (activeWorkspace === ctx.workspace) {
    activeWorkspace = null;
  }
  // Re-sync the Window menu (the user may have closed via X, not the menu).
  window.dispatchEvent(new CustomEvent(APP_EVT.WORKSPACE_CLOSED));
}

function tearDown(ctx) {
  if (ctx.unsubscribe) {
    ctx.unsubscribe();
    ctx.unsubscribe = null;
  }
  // While live-game windows survive, keep the workspace "active" so the
  // Window menu can still operate on them. Defer finalize until the last
  // live window closes.
  if (getLiveWindows().length > 0) {
    if (!ctx.liveWatcherAttached) {
      window.addEventListener(APP_EVT.LIVEGAME_CLOSED, ctx.onLiveGameClosed);
      ctx.liveWatcherAttached = true;
    }
    return;
  }
  finalize(ctx);
}

// Snapshot, mark explicit-close, force-close all standard windows,
// tear down. Used by both close() (navigate-away) and closeAll().
function dismissWindows(ctx, { markClosed, liveSnap } = {}) {
  const state = snapshot(ctx);
  if (liveSnap) state.live = liveSnap;
  if (markClosed) state._closed = true;
  saveState(ctx.tournament.id, state);
  ctx.explicitlyClosed = true;
  for (const k of Object.keys(ctx.windows)) {
    if (ctx.windows[k]) {
      ctx.windows[k].close(true);
      ctx.windows[k] = null;
    }
  }
  closeAllLiveGames();
  tearDown(ctx);
}

// Tournament-switch path: snapshot stays restorable (no _closed). Stale
// live windows close; resolved game-id windows persist (final banner).
// tearDown() defers finalize until those finally close.
function close(ctx) {
  dismissWindows(ctx, { markClosed: false });
}

// Window menu's Close All: explicit dismissal. Snapshot remains
// restorable via the ribbon, but _closed=true blocks navigation reopen.
function closeAll(ctx) {
  const liveSnap = snapshotLive(ctx);
  closeAllLiveGames();
  dismissWindows(ctx, { markClosed: true, liveSnap });
}

function focus(ctx) {
  for (const wb of openWindows(ctx)) {
    try { wb.focus(); } catch { /* */ }
  }
}

function hide(ctx) {
  detachResizeListeners(ctx);
  for (const wb of openWindows(ctx)) {
    try { wb.hide(); } catch { /* */ }
  }
}

function show(ctx) {
  for (const wb of openWindows(ctx)) {
    try { wb.show(); } catch { /* */ }
  }
  attachResizeListeners(ctx);
  requestAnimationFrame(() => reapplyLayout(ctx));
}

function isHidden(ctx) {
  const wbs = openWindows(ctx);
  return wbs.length > 0 && wbs.every(wb => wb.hidden);
}

function openSystemWindow(ctx, key, { flash = true } = {}) {
  if (ctx.windows[key]) {
    try {
      const wb = ctx.windows[key];
      if (wb.min) wb.restore();
      wb.focus();
      if (flash) flashWindow(wb);
    } catch {}
    return;
  }
  const spec = ctx.windowSpecs[key];
  const body = spec.makeBody();
  spec.setBody(body);
  ctx.windows[key] = makeBox(ctx, key, spec.title, body);
  spec.postCreate?.(ctx.windows[key]);
  spec.render();
  armSubscriptions(ctx);
  refresh(ctx);
  if (flash) requestAnimationFrame(() => { try { flashWindow(ctx.windows[key]); } catch {} });
  requestAnimationFrame(() => reapplyLayout(ctx));
}

async function initWorkspace(ctx) {
  await Promise.all([refresh(ctx), backfillEvents(ctx)]);
  if (!ctx.restoreFromSaved) {
    if (ctx.detail?.status === STATUS.RUNNING) openSystemWindow(ctx, "schedule", { flash: false });
    if (ctx.eventLog.length > 0 || ctx.detail?.status === STATUS.RUNNING) openSystemWindow(ctx, "log", { flash: false });
  }
  if (Array.isArray(ctx.savedState?.live) && ctx.savedState.live.length > 0) {
    const sorted = [...ctx.savedState.live].sort((a, b) => (a.z ?? 0) - (b.z ?? 0));
    const running = ctx.detail?.status === STATUS.RUNNING;
    let endedWhileAway = 0;
    for (const s of sorted) {
      // Don't restore minimized-window geometry -- it's the dock
      // position, not the pre-minimize rect.
      const rect = s.min ? null : { x: s.x, y: s.y, w: s.width, h: s.height };
      if (s.resolved) {
        // Resolved at snapshot time -- rehydrate from PGN, no WS.
        // Seed resolvedGames so subsequent snapshotLive() calls can
        // re-persist `resolved` (frozen windows don't produce live
        // game_reconciled events that would refill the map).
        if (s.gameId) ctx.resolvedGames.set(s.gameId, s.resolved);
        const fres = openFrozenGameWindow({
          proxyId: s.proxyId, gameId: s.gameId,
          label: s.label, engineName: s.engineName,
          token: ctx.token, tournamentId: ctx.tournament.id,
          gameN: s.resolved.gameN,
          result: s.resolved.result,
          termination: s.resolved.termination,
          top: ctx.top, left: ctx.left, right: ctx.getRightInset(),
          boardStyle: ctx.boardStyleCached,
          initialRect: rect, min: !!s.min, max: !!s.max, flash: false,
        });
        if (fres?.wb && !fres.alreadyOpen) wireLayoutHandlers(ctx, fres.wb);
      } else if (running) {
        // Live-reattach only if the server still considers this pair
        // alive; otherwise the WS would auto-close on first {ended}
        // and the user would see a window flash and vanish.
        const stillLive = s.gameId
          ? ctx.livePairings.get(s.proxyId)?.pairId === s.gameId
          : ctx.activeProxies.has(s.proxyId);
        if (stillLive) {
          attachWatch(ctx, null, s.gameId ?? s.proxyId, {
            proxyId: s.proxyId, gameId: s.gameId ?? null,
            label: s.label, engineName: s.engineName,
            initialRect: rect,
            min: !!s.min, max: !!s.max, flash: false,
          });
        } else {
          endedWhileAway++;
        }
      }
      else {
        // Not resolved, not running -- game ended while we had no
        // way to capture its resolution. Drop, count for toast.
        endedWhileAway++;
      }
    }
    if (endedWhileAway > 0) {
      const msg = endedWhileAway === 1
        ? "1 watched game finished while away."
        : `${endedWhileAway} watched games finished while away.`;
      toast(msg, { variant: "warning", duration: 7000 });
    }
    requestAnimationFrame(() => reapplyLayout(ctx));
  }
}


// ---- Rendering ----------------------------------------------------------

function renderStandings(ctx) {
  const sprtSlot = ctx.standingsBody.querySelector(".wb-sprt-slot");
  const partialSlot = ctx.standingsBody.querySelector(".wb-partial-slot");
  const emptyEl = ctx.standingsBody.querySelector(".wb-standings-empty");
  const wrapEl = ctx.standingsBody.querySelector(".wb-standings-table-wrap");
  const tbody = ctx.standingsBody.querySelector(".wb-standings-tbl tbody");
  const standings = ctx.detail?.standings;
  if (!standings || standings.engines.length === 0) {
    emptyEl.textContent = "No games played yet.";
    emptyEl.hidden = false;
    wrapEl.hidden = true;
    sprtSlot.innerHTML = "";
    partialSlot.innerHTML = "";
    return;
  }
  emptyEl.hidden = true;
  wrapEl.hidden = false;
  const sprt = ctx.detail.sprt;
  tbody.innerHTML = standings.engines
    .map((e) => {
      const eloCell = e.elo == null
        ? "--"
        : (e.elo >= 0 ? "+" : "") + e.elo.toFixed(1) +
          (e.elo_margin_95 == null ? "" : ` +/- ${e.elo_margin_95.toFixed(1)}`);
      const ordoCell = e.elo_ordo == null
        ? "--"
        : (e.elo_ordo >= 0 ? "+" : "") + e.elo_ordo.toFixed(1) +
          (e.elo_ordo_margin_95 == null ? "" : ` +/- ${e.elo_ordo_margin_95.toFixed(1)}`);
      return `
      <tr>
        <td class="wb-eng-name" title="${escapeHtml(e.name)}">${escapeHtml(e.name)}</td>
        <td>${e.games}</td>
        <td>${e.wins}</td>
        <td>${e.losses}</td>
        <td>${e.draws}</td>
        <td>${e.points}</td>
        <td>${(e.score_pct * 100).toFixed(1)}%</td>
        <td>${eloCell}</td>
        <td>${ordoCell}</td>
      </tr>`;
    })
    .join("");
  if (sprt) {
    const lo = sprt.lower_bound, hi = sprt.upper_bound, llr = sprt.llr;
    const concluded = sprt.status !== SPRT.CONTINUE;
    const colorMod = concluded ? (sprt.status === SPRT.H1 ? " wb-sprt--h1" : " wb-sprt--h0") : "";
    const candidate = ctx.detail.engines?.[0]?.name ? escapeHtml(ctx.detail.engines[0].name) : "candidate";
    const pairsText = sprt.pairs != null ? ` * ${sprt.pairs} pair${sprt.pairs === 1 ? "" : "s"}` : "";
    const statusText = sprt.status === SPRT.H1
      ? `H1 (${candidate} is stronger)`
      : sprt.status === SPRT.H0
        ? `H0 (no significant difference)`
        : sprt.status;
    sprtSlot.innerHTML = `<div class="wb-sprt${colorMod}">` +
      `SPRT ${candidate} [${sprt.elo0}, ${sprt.elo1}] * LLR=${llr.toFixed(2)} [${lo.toFixed(2)}, ${hi.toFixed(2)}]` +
      `${pairsText} * ${statusText}` +
      `</div>`;
  } else {
    sprtSlot.innerHTML = "";
  }
  const partialPairs = ctx.detail.partial_pairs ?? 0;
  // Hide during RUNNING -- a fresh game-1 always sits alone in the
  // PGN until game-2 of the pair finishes; that's normal, not data loss.
  const showPartial = partialPairs > 0 && ctx.detail.status !== STATUS.RUNNING;
  partialSlot.innerHTML = showPartial
    ? `<div class="wb-partial-pairs">${partialPairs} incomplete pair${partialPairs === 1 ? "" : "s"} ` +
      `(one game missing)</div>`
    : "";
}


function renderSchedule(ctx) {
  if (ctx.livePairings.size === 0) {
    // Pair confirmation can lag game start by seconds at fast tc;
    // distinguish "settling" (proxies up, no confirmed pairs yet)
    // from "really nothing running".
    const settling = (
      ctx.detail?.status === STATUS.RUNNING && ctx.activeProxies.size > 0
    );
    ctx.scheduleBody.innerHTML = settling
      ? `<div class="wb-empty">Starting up...</div>`
      : `<div class="wb-empty">No games in play.</div>`;
    return;
  }
  const scroller = ctx.scheduleBody.parentElement;
  const atBottom = !scroller || isPinnedToBottom(scroller, AUTOSCROLL_SLACK_ROW_PX);
  ctx.scheduleBody.innerHTML = `<ul class="wb-sched-list"></ul>`;
  const list = ctx.scheduleBody.querySelector(".wb-sched-list");

  // Dedupe: both proxies map to the same info object,
  // so skip if we already rendered this pair.
  const shownPairs = new Set();
  for (const [, info] of ctx.livePairings) {
    const key = [info.proxyA, info.proxyB].sort().join(":");
    if (shownPairs.has(key)) continue;
    shownPairs.add(key);
    const li = document.createElement("li");
    li.className = "wb-sched-live wb-sched-pair";
    const wLabel = info.sideA === SIDE.WHITE ? info.engineA : info.engineB;
    const bLabel = info.sideA === SIDE.WHITE ? info.engineB : info.engineA;
    const pairLabel = `${wLabel} - ${bLabel}`;
    li.innerHTML = `
      <span class="wb-sched-icon">&#9822;</span>
      <span class="wb-sched-game" title="${escapeHtml(pairLabel)}">${escapeHtml(pairLabel)}</span>
    `;
    const btn = document.createElement("button");
    btn.className = "wb-sched-attach-btn";
    btn.textContent = "watch";
    btn.title = info.pairId || key;
    btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(info.pairId || key));
    btn.addEventListener("click", () => attachWatch(ctx, btn, info.pairId || key, {
      proxyId: info.proxyA,
      gameId: info.pairId || null,
      label: `${wLabel} vs ${bLabel}`,
      engineName: wLabel,
    }));
    li.appendChild(btn);
    list.appendChild(li);
  }

  if (atBottom) scrollToBottom(scroller);
}

function renderEngines(ctx) {
  // One row per active proxy (engine process). Attach via proxy_id WS,
  // single-engine identity (survives book-line ambiguity where pair
  // confirmation hasn't happened yet). Distinct from Live Games which
  // is keyed on confirmed pair_ids.
  if (ctx.activeProxies.size === 0) {
    ctx.enginesBody.innerHTML = `<div class="wb-empty">No active engines.</div>`;
    return;
  }
  const scroller = ctx.enginesBody.parentElement;
  const atBottom = !scroller || isPinnedToBottom(scroller, AUTOSCROLL_SLACK_ROW_PX);
  ctx.enginesBody.innerHTML = `<ul class="wb-sched-list"></ul>`;
  const list = ctx.enginesBody.querySelector(".wb-sched-list");
  for (const [pid, p] of ctx.activeProxies) {
    const li = document.createElement("li");
    li.className = "wb-sched-live";
    const engineLabel = p.engineName || pid;
    li.innerHTML = `
      <span class="wb-sched-icon">&#9881;</span>
      <span class="wb-sched-game" title="${escapeHtml(engineLabel)}">${escapeHtml(engineLabel)}</span>
    `;
    const btn = document.createElement("button");
    btn.className = "wb-sched-attach-btn";
    btn.textContent = "watch";
    btn.title = pid;
    btn.classList.toggle("wb-sched-attach-btn--live", isLiveWindowOpen(pid));
    btn.addEventListener("click", () => attachWatch(ctx, btn, pid, {
      proxyId: pid,
      label: `${engineLabel}`,
      engineName: engineLabel,
    }));
    li.appendChild(btn);
    list.appendChild(li);
  }
  if (atBottom) scrollToBottom(scroller);
}

// rAF-coalesced render: at fast TC the runner_log stream can drive
// hundreds of renders/sec; without this the main thread wedges and
// button clicks feel dead. flagKey is a per-render pending flag on ctx.
function scheduleRender(ctx, flagKey, renderFn) {
  if (ctx[flagKey]) return;
  ctx[flagKey] = true;
  requestAnimationFrame(() => { ctx[flagKey] = false; renderFn(ctx); });
}

function renderErrorBanner(ctx) {
  const banner = ctx.logBody.querySelector(".wb-error-banner");
  if (banner) {
    const err = ctx.detail?.last_error;
    if (err) {
      // Server-emitted server-crash hint vs fastchess's own runner-crash
      // hint. Both produce a Restart button in the banner; we just need
      // to strip the source-side "press Start" prose so we don't show it
      // twice (once as text, once as the button).
      const RESTART_HINT_SUFFIX = "press Start to restart from scratch (prior games will be discarded).";
      const FASTCHESS_RESUME_LINE = "To resume the tournament, run:";
      const rawTail = (err.stderr_tail || []).slice(-10);
      const lastIdx = rawTail.length - 1;

      let displayLines = rawTail;
      let restartPre = null;
      if (lastIdx >= 0 && rawTail[lastIdx].includes(RESTART_HINT_SUFFIX)) {
        const head = rawTail[lastIdx].slice(0, rawTail[lastIdx].indexOf(RESTART_HINT_SUFFIX));
        const trimmed = head.replace(/[;\s]+$/, "");
        displayLines = trimmed
          ? [...rawTail.slice(0, lastIdx), trimmed]
          : rawTail.slice(0, lastIdx);
        restartPre = "; press ";
      } else {
        const i = rawTail.findIndex((l) => l.includes(FASTCHESS_RESUME_LINE));
        if (i >= 0) {
          displayLines = rawTail.slice(0, i);
          restartPre = " -- press ";
        }
      }
      // FAILED always means "won't resume itself", so Restart is
      // always a valid action. Default unconditionally so an
      // unrecognized failure shape doesn't dead-end the workspace.
      if (restartPre === null) restartPre = "; press ";

      const tail = displayLines.join("\n") || `exit code ${err.rc}`;
      banner.innerHTML = `<div class="wb-error-title">Tournament failed (rc=${err.rc})</div><pre>${escapeHtml(tail)}</pre>`;
      if (restartPre) {
        const blocked = ctx.otherActiveId != null;
        const otherLabel = ctx.otherActiveName ? `"${ctx.otherActiveName}"` : "another tournament";
        const tooltip = blocked
          ? `${otherLabel} is currently running. Stop it first.`
          : "Restart";
        const trailing = blocked
          ? ` -- stop ${otherLabel} first to restart.`
          : " to restart from scratch.";
        const pre = banner.querySelector("pre");
        pre.append(restartPre);
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "toast-icon-btn";
        btn.setAttribute("aria-label", "Restart");
        btn.setAttribute("title", tooltip);
        btn.disabled = blocked;
        const ic = document.createElement("wa-icon");
        ic.setAttribute("name", "rotate-right");
        btn.appendChild(ic);
        btn.addEventListener("click", async () => {
          // Re-read ctx.otherActiveId at click time. btn.disabled is
          // latched at render and can lag a setOtherActive update.
          if (ctx.otherActiveId != null) {
            const lbl = ctx.otherActiveName ? `"${ctx.otherActiveName}"` : "another tournament";
            toast(`${lbl} is currently running. Stop it first.`, { variant: "warning" });
            return;
          }
          const ok = await confirm(buildRestartConfirm(ctx.tournament.name, ctx.detail?.standings?.games ?? 0));
          if (!ok) return;
          try {
            await ctx.api("POST", `/api/tournaments/${ctx.tournament.id}/start?${CONFIRM_WIPE_QS}`);
          } catch (e) {
            toast(`Restart failed: ${apiErrorDetail(e)}`, { variant: "danger" });
          }
        });
        pre.appendChild(btn);
        pre.append(trailing);
      }
      banner.hidden = false;
    } else {
      banner.hidden = true;
      banner.innerHTML = "";
    }
  }
}

function renderEventLogList(ctx) {
  const list = ctx.logBody.querySelector(".wb-eventlog-list");
  if (!list) return;
  const scroller = ctx.logBody.parentElement;
  const atBottom = !scroller || isPinnedToBottom(scroller, AUTOSCROLL_SLACK_ROW_PX);
  list.innerHTML = ctx.eventLog.filter(e => e.payload?.kind !== KIND.PROXY_UNPAIRED).map((e) => {
    const ts = e.ts || "";
    const inner = e.payload?.kind;
    // runner_log: surface the actual fastchess stdout/stderr line.
    if (inner === KIND.RUNNER_LOG && e.payload?.line) {
      const stream = e.payload.stream === "err" ? " err" : "";
      return `<li><span class="wb-log-ts">${ts}</span>` +
        `<span class="wb-log-runner${stream}">${escapeHtml(e.payload.line)}</span></li>`;
    }
    // Muted ctx.detail parts appended after the primary kind label.
    const parts = [];
    if (e.kind === EVT.STATUS && e.payload?.status)
      parts.push(e.payload.status);
    else if (inner === KIND.GAME_FINISHED) {
      const a = e.payload?.engine_a || "?";
      const b = e.payload?.engine_b || "?";
      const result = e.payload?.result;
      const termination = e.payload?.termination;
      const tail = (result && termination && termination !== "unknown")
        ? `${result} ${termination}` : (result || "");
      const gn = e.payload?.game_n;
      const head = (gn != null) ? `${inner} #${gn}` : inner;
      parts.push(head, `${a} vs ${b}`, ...(tail ? [tail] : []));
    } else if (inner === KIND.PROXY_PAIRED) {
      const a = e.payload?.engine_a || "?";
      const b = e.payload?.engine_b || "?";
      const pa = e.payload?.proxy_a || "";
      const pb = e.payload?.proxy_b || "";
      parts.push(inner, `${a}(${pa}) vs ${b}(${pb})`);
    } else if (inner === KIND.PROXY_UNPAIRED) {
      const pa = e.payload?.proxy_id || "";
      const pb = e.payload?.peer_id  || "";
      parts.push(inner, pa, pb);
    } else if (inner === KIND.PROXY_STARTED) {
      parts.push(inner);
      if (e.payload?.engine_name) parts.push(e.payload.engine_name);
    } else if (inner === KIND.RUNNER_CRASH) {
      parts.push(inner);
      if (e.payload?.rc != null) parts.push(`rc=${e.payload.rc}`);
    } else if (inner) {
      parts.push(inner);
    }
    const detailHtml = parts.map(p => ` <span class="wb-log-detail">${escapeHtml(p)}</span>`).join("");
    return `<li><span class="wb-log-ts">${ts}</span> <span class="wb-log-kind">${escapeHtml(e.kind)}</span>${detailHtml}</li>`;
  }).join("");
  if (atBottom) scrollToBottom(scroller);
}

function renderEventLog(ctx) {
  renderErrorBanner(ctx);
  renderEventLogList(ctx);
}


// ---- Window spec table --------------------------------------------------

function buildWindowSpecs(ctx) {
  return {
    standings: {
      title: "Standings",
      makeBody: makeStandingsBody,
      setBody: (b) => { ctx.standingsBody = b; },
      render: () => renderStandings(ctx),
    },
    schedule: {
      title: "Live Games",
      makeBody: makeScheduleBody,
      setBody: (b) => { ctx.scheduleBody = b; },
      render: () => renderSchedule(ctx),
      // Stable scrollbar gutter so the panel doesn't pulse in width as
      // live-game rows come and go.
      extraClass: "sturddle-wb-live-games",
    },
    engines: {
      title: "Engine Instances",
      makeBody: makeEnginesBody,
      setBody: (b) => { ctx.enginesBody = b; },
      render: () => renderEngines(ctx),
    },
    log: {
      title: "Event Log",
      makeBody: makeLogBody,
      setBody: (b) => { ctx.logBody = b; },
      render: () => renderEventLog(ctx),
      postCreate: (wb) => {
        wb.addControl({
          class: "wb-log-copy-ctrl",
          index: 0,
          click: () => {
            const text = ctx.eventLog
              .filter(e => e.payload?.kind !== KIND.PROXY_UNPAIRED)
              .map(e => {
                const ts = e.ts || "";
                const inner = e.payload?.kind;
                const line = e.payload?.line;
                if (inner === KIND.RUNNER_LOG && line) return `${ts} ${line}`;
                const parts = [e.kind];
                if (inner) parts.push(inner);
                if (inner === KIND.PROXY_PAIRED)
                  parts.push(`${e.payload?.engine_a}(${e.payload?.proxy_a||""}) vs ${e.payload?.engine_b}(${e.payload?.proxy_b||""})`);
                else if (inner === KIND.GAME_FINISHED) {
                  const result = e.payload?.result;
                  const termination = e.payload?.termination;
                  const tail = (result && termination && termination !== "unknown")
                    ? `${result} ${termination}` : (result || "");
                  const gn = e.payload?.game_n;
                  if (gn != null) parts[parts.length - 1] = `${parts[parts.length - 1]} #${gn}`;
                  parts.push(`${e.payload?.engine_a} vs ${e.payload?.engine_b}`,
                             ...(tail ? [tail] : []));
                }
                return `${ts} ${parts.join(" ")}`;
              }).join("\n");
            navigator.clipboard.writeText(text)
              .then(() => toast("Event log copied to clipboard", { variant: "success", duration: 1500 }))
              .catch(() => {});
          },
        });
        wb.g.querySelector(".wb-log-copy-ctrl").title = "Copy event log";
      },
    },
  };
}


function loadState(id) {
  return loadJson(STORAGE_KEY_PREFIX + id);
}

function saveState(id, state) {
  saveJson(STORAGE_KEY_PREFIX + id, state);
}

function hasOpenWindows(state) {
  return state !== null &&
    Object.entries(state).some(([k, v]) => k !== "_closed" && v?.open);
}

// Restorable: snapshot has open windows AND was not explicitly dismissed.
// Navigation uses this to decide whether to reopen.
export function hasSavedWorkspaceState(id) {
  const s = loadState(id);
  return s !== null && !s._closed && hasOpenWindows(s);
}

// Distinguishes "brand-new tournament" from "explicitly dismissed".
export function hasAnyDesktopState(id) {
  return loadState(id) !== null;
}

export function clearWorkspaceState(id) {
  removeKey(STORAGE_KEY_PREFIX + id);
}


const LAYOUT = Object.freeze({ NONE: 0, TIDY: 1, TILE: 2, SNAP: 3 });

// Single active workspace (single-active model). Per-workspace layout
// lives on ctx.activeLayout; the module-level exports below read it
// through the active workspace so other modules see the live value.
let activeWorkspace = null;
function setLayout(ctx, mode) {
  ctx.activeLayout = mode;
}


// Wiring shell: builds the shared `ctx` state object, opens the initial
// windows, registers listeners, and returns the workspace control API.
// The behavior lives in the module-level helpers that take `ctx`.
export function openTournamentWorkspace({ api, events, log, token, tournament, top = 0, left = 0, getRight = () => window.innerWidth }) {
  // Single-active model. Re-clicking the workspace icon for the
  // already-open tournament is a no-op (just focus its windows) so
  // attached engine windows survive -- closing here would tear them
  // down via tearDown's closeAllLiveGames().
  if (activeWorkspace) {
    if (activeWorkspace.tournamentId === tournament.id) return activeWorkspace;
    activeWorkspace.close();
    activeWorkspace = null;
  }

  const savedState = loadState(tournament.id);
  // Restore-from-snapshot when there's any open window in the snapshot,
  // regardless of _closed (the ribbon always restores; _closed only blocks
  // navigation). lastGeometry holds last-known position/size per key so
  // closed slots can carry geometry forward into the next snapshot.
  const restoreFromSaved = hasOpenWindows(savedState);
  const initialLayout = restoreFromSaved ? (savedState._layout ?? LAYOUT.NONE) : LAYOUT.NONE;
  const lastGeometry = {};
  for (const key of ["standings", "schedule", "engines", "log"]) {
    const s = savedState?.[key];
    lastGeometry[key] = s
      ? { x: s.x, y: s.y, width: s.width, height: s.height }
      : null;
  }
  const eventLog = [];
  // Server-stamped sequence numbers we've already added to eventLog.
  // Lets us run the WS subscription in parallel with the REST backfill
  // without showing duplicates around workspace open.
  const seenSeqs = new Set();
  // proxy_id -> { engineName }
  const activeProxies = new Map();
  // proxy_id -> { pairId, proxyA, engineA, sideA, proxyB, engineB, sideB }
  // Both proxies in a pair map to the same info object.
  const livePairings = new Map();
  // pair_id -> { gameN, result, termination }. Populated from
  // game_reconciled so snapshotLive() can mark resolved windows for
  // frozen-rehydration on a later workspace re-open.
  const resolvedGames = new Map();


  // Slot grid hands out aligned rects for live-board windows. A slot is
  // free if no live window currently overlaps it, so dragging a window
  // out of its slot frees that slot without explicit bookkeeping. When
  // no slot fits, the new window is minimized -- WS still connects so
  // the live state stays current behind the minimize bar.
  const MAX_GRID_COLS = 4;
  const slotGrid = createSlotGrid({
    top, left, getRight,
    getCellWidth: () => {
      const cols = Math.min(MAX_GRID_COLS, Number(ctx.detail?.template?.games_in_parallel) || MAX_GRID_COLS);
      return Math.max(LIVE_MIN_WIDTH, Math.floor((getRight() - left - SLOT_GAP * (cols - 1)) / cols));
    },
    cellHeight: LIVE_MIN_HEIGHT(),
    getWindows: () => getLiveWindows(),
    getMaxRows: () => ctx.activeLayout === LAYOUT.TIDY ? 1 : Infinity,
  });

  // Shared state for the module-level workspace helpers, which all take it
  // as their first argument. Carries config, the by-reference collections
  // (mutated in place, never reassigned), the slot grid, the reassigned
  // scalars (detail, body refs, lifecycle guards), and the bound listener
  // thunks (added/removed by the same reference).
  const ctx = {
    api, events, log, token, tournament, top, left, getRight,
    savedState, restoreFromSaved, slotGrid,
    lastGeometry, eventLog, seenSeqs,
    activeProxies, livePairings, resolvedGames,
    windows: { standings: null, schedule: null, engines: null, log: null },
    activeLayout: initialLayout,
    boardStyleCached: null, detail: null,
    otherActiveId: null, otherActiveName: null,
    _schedulePending: false, _enginesPending: false, _eventLogPending: false,
    unsubscribe: null,
    // True when close() / closeAll() drove the tear-down. Distinguishes from
    // "user closed the last window manually" -- in that case finalize() is the
    // one that writes the snapshot (with _closed=true).
    explicitlyClosed: false,
    // Idempotency guard: tearDown can be reached via close() and again via the
    // last onclose callback; finalize() must run exactly once.
    finalized: false,
    liveWatcherAttached: false,
    resizeTimer: null,
  };
  // Body refs live on ctx: reassigned when a window is re-opened after the
  // user closed it (windowSpecs.setBody), so renderers re-read ctx.* to
  // target the current body.
  ctx.standingsBody = makeStandingsBody();
  ctx.scheduleBody = makeScheduleBody();
  ctx.logBody = makeLogBody();
  ctx.enginesBody = makeEnginesBody();

  // Board style is fetched once per workspace open and reused for every
  // watch click. Avoids a /settings round-trip on each click and keeps
  // all live windows in this session visually consistent even if the
  // user changes the global setting mid-tournament.
  api("GET", "/settings")
    .then(s => { ctx.boardStyleCached = s?.board_style || null; })
    .catch(() => {});
  // ---- Window construction ----------------------------------------------

  // getRightInset: inset (px) from the right viewport edge to the workspace
  // area -- ribbon width when docked right, 0 when docked left. WinBox's
  // maximize() respects this so a maximized window stops at the ribbon.
  ctx.getRightInset = () => Math.max(0, window.innerWidth - getRight());
  ctx.pendingDragX = null;

  const windowSpecs = buildWindowSpecs(ctx);
  ctx.windowSpecs = windowSpecs;

  if (restoreFromSaved) {
    // Recreate in saved z-order so the highest-z slot ends up topmost.
    const openKeys = Object.keys(ctx.windows)
      .filter(k => savedState[k]?.open)
      .sort((a, b) => (savedState[a].z ?? 0) - (savedState[b].z ?? 0));
    for (const key of openKeys) {
      const s = savedState[key];
      const spec = windowSpecs[key];
      const body = spec.makeBody();
      spec.setBody(body);
      ctx.windows[key] = makeBox(ctx, key, spec.title, body, { min: s.min, max: s.max });
      spec.postCreate?.(ctx.windows[key]);
    }
  } else {
    // Default: standings only; initWorkspace opens more based on tournament status.
    ctx.windows.standings = makeBox(ctx, "standings", windowSpecs.standings.title, ctx.standingsBody);
  }
  requestAnimationFrame(() => reapplyLayout(ctx));

  // ---- Data refresh -----------------------------------------------------

  // Subscribe before backfill so any events firing during the REST
  // round-trip are captured (deduped against backfill via _seq).
  ctx.unsubscribe = ctx.events.on((evt) => pushEvent(ctx, evt));

  // Bound listener thunks: one reference per handler, stored on ctx so
  // add/removeEventListener use the SAME function (symmetry for removal).
  ctx.onReconnect = (e) => onReconnect(ctx, e);
  ctx.onBeforeUnload = () => onBeforeUnload(ctx);
  ctx.onRefreshWatchButtons = () => refreshWatchButtons(ctx);
  ctx.onLiveGameClosedReapply = () => onLiveGameClosedReapply(ctx);
  ctx.onResize = () => onResize(ctx);
  ctx.onLiveGameClosed = () => onLiveGameClosed(ctx);

  initWorkspace(ctx);

  window.addEventListener("beforeunload", ctx.onBeforeUnload);
  window.addEventListener(APP_EVT.CONNECTION, ctx.onReconnect);
  window.addEventListener(APP_EVT.LIVEGAME_CLOSED, ctx.onRefreshWatchButtons);
  window.addEventListener(APP_EVT.LIVEGAME_CLOSED, ctx.onLiveGameClosedReapply);
  attachResizeListeners(ctx);

  const workspace = {
    close: () => close(ctx),
    tile: (w, o) => tile(ctx, w, o),
    tidy: (o) => tidy(ctx, o),
    untidy: () => untidy(ctx),
    snap: () => snap(ctx),
    closeAll: () => closeAll(ctx),
    minimizeAll: () => minimizeAll(ctx),
    restoreWindows: (wbs) => restoreWindows(wbs),
    focus: () => focus(ctx),
    hide: () => hide(ctx),
    show: () => show(ctx),
    isHidden: () => isHidden(ctx),
    openSystemWindow: (key, o) => openSystemWindow(ctx, key, o),
    refresh: () => refresh(ctx),
    applyDetail: (fresh) => applyDetail(ctx, fresh),
    setOtherActive: (id, name) => setOtherActive(ctx, id, name),
    tournamentId: tournament.id,
    get layout() { return ctx.activeLayout; },
    get isTidy() { return ctx.activeLayout === LAYOUT.TIDY; },
  };
  ctx.workspace = workspace;
  activeWorkspace = workspace;
  return workspace;
}

export function getActiveWorkspace() {
  return activeWorkspace;
}

export function getActiveLayout() {
  return activeWorkspace?.layout ?? LAYOUT.NONE;
}

export { LAYOUT };
