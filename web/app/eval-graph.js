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
  fmtScore,
  isPinnedToBottom,
  rafCoalesce,
  scrollToBottom,
} from "./wb-utils.js";

// Bars saturate at this many centipawns; mate scores pin to it.
const CLAMP_CP = 750;
const BAR_THICKNESS_PX = 23;
const BAR_GAP_PX = 1;
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

export function createEvalGraph() {
  const el = document.createElement("div");
  el.className = "lg-eval-graph";
  const canvas = document.createElement("canvas");
  el.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  let samples = []; // index = ply, value = {cp (mover POV), w: moverIsWhite, label} (sparse)
  let firstPly = null;
  let visible = false;
  let stickToBottom = false; // force-scroll to newest on next draw (reveal)
  const rootStyle = getComputedStyle(document.documentElement);
  const labelFont = `${LABEL_FONT_PX}px ${
    rootStyle.getPropertyValue("--wa-font-family-code") || "monospace"}`;
  const offBarLabelColor = rootStyle.getPropertyValue("--accent").trim() || COLOR_LABEL;

  const draw = rafCoalesce(() => {
    const w = el.clientWidth;
    if (!w) return;
    const count = firstPly == null ? 0 : samples.length - firstPly;
    const h = Math.max(el.clientHeight, count * BAR_THICKNESS_PX);
    const pinned = isPinnedToBottom(el, AUTOSCROLL_SLACK_ROW_PX);
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(w * dpr);
    const ph = Math.round(h * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    canvas.style.height = `${h}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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

  const ro = new ResizeObserver(() => { if (visible) draw(); });
  ro.observe(el);

  // `score` is the unified {cp|mate} shape, mover (STM) POV. Returns
  // false when it carries nothing plottable.
  function add(ply, score, moverIsWhite) {
    let cp;
    if (score?.mate != null) cp = score.mate >= 0 ? CLAMP_CP : -CLAMP_CP;
    else if (score?.cp != null) cp = score.cp;
    else return false;
    if (firstPly == null || ply < firstPly) firstPly = ply;
    samples[ply] = { cp, w: !!moverIsWhite, label: fmtScore(score, { signed: true }) };
    if (visible) draw();
    return true;
  }

  function clear() {
    samples = [];
    firstPly = null;
    if (visible) draw();
  }

  function setVisible(v) {
    visible = !!v;
    if (visible) {
      stickToBottom = true;
      draw();
    }
  }

  function dispose() {
    draw.cancel();
    ro.disconnect();
  }

  return { el, add, clear, setVisible, dispose };
}
