// Eval-over-moves barchart for live tournament games: one horizontal bar
// per ply, running top to bottom from the first observed ply, mover-POV
// (STM) centipawns against a vertical zero midline -- a bar extends
// right when the side that moved judged itself ahead, left when behind,
// scaled to the panel's width. Fill marks the mover: light = white,
// dark outlined = black. Bars have fixed thickness; when the game
// outgrows the panel the chart scrolls, sticking to the newest ply
// unless the user scrolled away. Samples accumulate while hidden (cheap
// array writes) so the chart has history on reveal; drawing only
// happens while visible, rAF-coalesced.

import {
  AUTOSCROLL_SLACK_ROW_PX,
  fmtMoveNo,
  fmtScore,
  isPinnedToBottom,
  isPinnedToRight,
  rafCoalesce,
  scrollToBottom,
  scrollToRight,
} from "./wb-utils.js";

// Bars saturate at this many centipawns; mate scores pin to it.
const CLAMP_CP = 750;
const BAR_THICKNESS_PX = 23;
const BAR_GAP_PX = 1;
// Play strip only: floor on bar length so near-zero evals still show a nub.
const BAR_MIN_LEN_PX = 3;
const COLOR_WHITE_BAR = "#e8e8e8";
const COLOR_BLACK_BAR = "#111";
const COLOR_BLACK_BAR_EDGE = "#777"; // outline so black bars read on dark bg
const COLOR_MIDLINE = "#555";
// Numeric label: on the bar (contrast color) when it fits, else just
// past the bar's tip in muted gray.
const COLOR_LABEL = "#e8e8e8";
const COLOR_LABEL_ON_WHITE = "#111";
const COLOR_LABEL_ON_BLACK = "#e8e8e8";
const LABEL_FONT_PX = 14;
const LABEL_PAD_PX = 4;

// Monospace label font + the off-bar label color, read from CSS vars.
function readLabelStyle() {
  const rootStyle = getComputedStyle(document.documentElement);
  const labelFont = `${LABEL_FONT_PX}px ${
    rootStyle.getPropertyValue("--wa-font-family-code") || "monospace"}`;
  const offBarLabelColor = rootStyle.getPropertyValue("--muted-bright").trim() || COLOR_LABEL;
  return { labelFont, offBarLabelColor };
}

// {cp|mate} (any POV) -> centipawns clamped to +/-CLAMP_CP, or null when
// the score carries nothing plottable. Mate pins to the saturation value.
function scoreToClampedCp(score) {
  if (score?.mate != null) return score.mate >= 0 ? CLAMP_CP : -CLAMP_CP;
  if (score?.cp != null) return score.cp;
  return null;
}

// Size the canvas backing store to CSS-pixels * devicePixelRatio and map
// the 2D context back to CSS units, so drawing code works in CSS pixels.
function resizeCanvasForDpr(canvas, ctx, w, h) {
  const dpr = window.devicePixelRatio || 1;
  const pw = Math.round(w * dpr);
  const ph = Math.round(h * dpr);
  if (canvas.width !== pw || canvas.height !== ph) {
    canvas.width = pw;
    canvas.height = ph;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

// Shared rig for a DPR-aware canvas widget that only paints while visible:
// owns the element/canvas/context, the visibility flag, and the
// ResizeObserver -> redraw wiring. Callers supply the draw fn via setDraw
// and an optional onReveal hook (fired just before the reveal repaint).
function createCanvasWidget(className, { onReveal } = {}) {
  const el = document.createElement("div");
  el.className = className;
  const canvas = document.createElement("canvas");
  el.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  let visible = false;
  let draw = () => {};
  const ro = new ResizeObserver(() => { if (visible) draw(); });
  ro.observe(el);
  return {
    el,
    canvas,
    ctx,
    isVisible: () => visible,
    setDraw: (fn) => { draw = fn; },
    setVisible(v) {
      visible = !!v;
      if (visible) {
        onReveal?.();
        draw();
      }
    },
    dispose() {
      draw.cancel?.();
      ro.disconnect();
    },
  };
}

export function createEvalGraph() {
  let samples = []; // index = ply, value = {cp (mover POV), w: moverIsWhite, label} (sparse)
  let firstPly = null;
  let stickToBottom = false; // force-scroll to newest on next draw (reveal)
  const rig = createCanvasWidget("lg-eval-graph", { onReveal: () => { stickToBottom = true; } });
  const { el, canvas, ctx } = rig;
  const { labelFont, offBarLabelColor } = readLabelStyle();

  const draw = rafCoalesce(() => {
    const w = el.clientWidth;
    if (!w) return;
    const count = firstPly == null ? 0 : samples.length - firstPly;
    const h = Math.max(el.clientHeight, count * BAR_THICKNESS_PX);
    const pinned = isPinnedToBottom(el, AUTOSCROLL_SLACK_ROW_PX);
    resizeCanvasForDpr(canvas, ctx, w, h);
    canvas.style.height = `${h}px`;
    ctx.clearRect(0, 0, w, h);
    const mid = w / 2;
    ctx.fillStyle = COLOR_MIDLINE;
    ctx.fillRect(Math.round(mid) - 0.5, 0, 1, h);
    const bh = BAR_THICKNESS_PX - BAR_GAP_PX;
    ctx.font = labelFont;
    ctx.textBaseline = "middle";
    for (let ply = firstPly ?? 0; ply < samples.length; ply++) {
      const s = samples[ply];
      if (s == null) continue;
      const frac = Math.max(-1, Math.min(1, s.cp / CLAMP_CP));
      const bw = Math.max(1, Math.abs(frac) * (mid - 1));
      const x = frac >= 0 ? mid : mid - bw;
      const y = (ply - firstPly) * BAR_THICKNESS_PX;
      ctx.fillStyle = s.w ? COLOR_WHITE_BAR : COLOR_BLACK_BAR;
      ctx.fillRect(x, y, bw, bh);
      if (!s.w) {
        ctx.strokeStyle = COLOR_BLACK_BAR_EDGE;
        ctx.lineWidth = 1;
        ctx.strokeRect(x + 0.5, y + 0.5, Math.max(1, bw - 1), Math.max(1, bh - 1));
      }
      // Label on the bar when it fits, else just past its tip.
      const tw = ctx.measureText(s.label).width;
      const onBar = bw >= tw + LABEL_PAD_PX * 2;
      ctx.textAlign = frac >= 0 ? "left" : "right";
      if (onBar) {
        ctx.fillStyle = s.w ? COLOR_LABEL_ON_WHITE : COLOR_LABEL_ON_BLACK;
        ctx.fillText(s.label, frac >= 0 ? mid + LABEL_PAD_PX : mid - LABEL_PAD_PX, y + bh / 2);
      } else {
        ctx.fillStyle = offBarLabelColor;
        ctx.fillText(s.label, frac >= 0 ? mid + bw + LABEL_PAD_PX : mid - bw - LABEL_PAD_PX, y + bh / 2);
      }
    }
    if (pinned || stickToBottom) {
      stickToBottom = false;
      scrollToBottom(el);
    }
  });
  rig.setDraw(draw);

  // `score` is the unified {cp|mate} shape, mover (STM) POV. Returns
  // false when it carries nothing plottable.
  function add(ply, score, moverIsWhite) {
    const cp = scoreToClampedCp(score);
    if (cp == null) return false;
    if (firstPly == null || ply < firstPly) firstPly = ply;
    samples[ply] = { cp, w: !!moverIsWhite, label: fmtScore(score, { signed: true }) };
    if (rig.isVisible()) draw();
    return true;
  }

  function clear() {
    samples = [];
    firstPly = null;
    if (rig.isVisible()) draw();
  }

  return { el, add, clear, setVisible: rig.setVisible, dispose: rig.dispose };
}

// Marks a strip with nothing plotted (view mode / pre-eval play). The play
// dock hides slots holding an empty strip and counts them out of dock-empty
// accounting; CSS hides the slot itself.
export const EVAL_EMPTY_CLASS = "eval-empty";

// Horizontal eval strip for the Play perspective: one vertical bar per
// engine ply, running left to right, engine-POV centipawns against a
// horizontal zero midline -- a bar grows up when the engine judged
// itself ahead, down when behind, scaled to the strip's current height
// (so it tracks layout reflow). Only the engine's plies get bars; the
// human's are skipped. Fill marks the engine's color: light = white,
// dark outlined = black. Bars have fixed width; when the game outgrows
// the strip it scrolls, sticking to the newest ply unless scrolled away.
export function createEvalBar({ onBarClick = null, isBarNavigable = null } = {}) {
  let samples = []; // [{cp (engine POV), w: engineIsWhite, label, ply}]
  let stickToRight = false; // force-scroll to newest on next draw (reveal)
  const rig = createCanvasWidget("game-view-eval-bar", { onReveal: () => { stickToRight = true; } });
  const { el, canvas, ctx } = rig;
  el.classList.add(EVAL_EMPTY_CLASS);
  // One fill for every bar: the engine's side is constant per game (and
  // shown on the board), so color is spent on contrast, not on side.
  const barFill = getComputedStyle(document.documentElement)
    .getPropertyValue("--accent-line").trim() || COLOR_WHITE_BAR;

  const barAt = (offsetX) => samples[Math.floor(offsetX / BAR_THICKNESS_PX)];

  // Hovered bar's score as a native tooltip. offsetX is in canvas CSS
  // pixels (= draw space), so it's scroll-independent.
  canvas.addEventListener("mousemove", (e) => {
    const s = barAt(e.offsetX);
    canvas.title = s ? `${fmtMoveNo(s.ply, s.w)} ${s.label}` : "";
    if (onBarClick) canvas.style.cursor = (s && (!isBarNavigable || isBarNavigable(s.ply))) ? "pointer" : "";
  });

  // Click a bar -> navigate to that ply (same as clicking the move). The
  // stored ply is the engine move's 0-based index (human plies have no bar).
  if (onBarClick) {
    canvas.addEventListener("click", (e) => {
      const s = barAt(e.offsetX);
      if (s) onBarClick(s.ply);
    });
  }

  const draw = rafCoalesce(() => {
    const h = el.clientHeight;
    if (!h) return;
    const w = Math.max(el.clientWidth, samples.length * BAR_THICKNESS_PX);
    const pinned = isPinnedToRight(el, AUTOSCROLL_SLACK_ROW_PX);
    resizeCanvasForDpr(canvas, ctx, w, h);
    canvas.style.width = `${w}px`;
    ctx.clearRect(0, 0, w, h);
    const mid = h / 2;
    ctx.fillStyle = COLOR_MIDLINE;
    ctx.fillRect(0, Math.round(mid) - 0.5, w, 1);
    const bw = BAR_THICKNESS_PX - BAR_GAP_PX;
    for (let i = 0; i < samples.length; i++) {
      const s = samples[i];
      const frac = Math.max(-1, Math.min(1, s.cp / CLAMP_CP));
      const bl = Math.max(BAR_MIN_LEN_PX, Math.abs(frac) * (mid - 1));
      const x = i * BAR_THICKNESS_PX;
      const y = frac >= 0 ? mid - bl : mid; // grow up when engine is ahead
      ctx.fillStyle = barFill;
      ctx.fillRect(x, y, bw, bl);
    }
    if (pinned || stickToRight) {
      stickToRight = false;
      scrollToRight(el);
    }
  });
  rig.setDraw(draw);

  // `items` is the engine's plies in order: {score (unified {cp|mate},
  // engine POV), ply (0-based move index)}. Entries that carry nothing
  // plottable are dropped. `engineIsWhite` fixes every bar's fill.
  function setSamples(items, engineIsWhite) {
    const next = [];
    for (const { score, ply } of items) {
      const cp = scoreToClampedCp(score);
      if (cp == null) continue;
      next.push({ cp, w: !!engineIsWhite, label: fmtScore(score, { signed: true }), ply });
    }
    const grew = next.length > samples.length;
    samples = next;
    el.classList.toggle(EVAL_EMPTY_CLASS, samples.length === 0);
    if (grew) stickToRight = true;
    if (rig.isVisible()) draw();
  }

  function clear() {
    samples = [];
    el.classList.add(EVAL_EMPTY_CLASS);
    if (rig.isVisible()) draw();
  }

  return { el, setSamples, clear, setVisible: rig.setVisible, dispose: rig.dispose };
}
