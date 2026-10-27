// Board-side eval gauge: White's share of the bar is a logistic win estimate
// of the shown score. Desktop: vertical, White's end toward White's pieces;
// mobile: horizontal under the board, White's end left (right when flipped).

import { SIDE } from "./chess-consts.js";
import { sidePovScore } from "./wb-utils.js";

// Lichess cp -> win-chance logistic slope.
const WIN_CHANCE_SLOPE = 0.00368208;
const SHARE_VAR = "--white-share";
const FLIPPED_CLASS = "flipped";
const EMPTY_CLASS = "is-empty";

export const EVAL_GAUGE_HTML = `
  <div class="eval-gauge ${EMPTY_CLASS}" aria-hidden="true">
    <div class="eval-gauge-fill"></div>
  </div>`;

// Mate 0 = the side to move is mated; the wire drops its sign.
function whiteShare(score, whiteToMove) {
  if (score.mate === 0) return whiteToMove ? 0 : 1;
  if (score.mate != null) return score.mate > 0 ? 1 : 0;
  return 1 / (1 + Math.exp(-WIN_CHANCE_SLOPE * score.cp));
}

// `pov` is the side `score` is from; null score empties the gauge.
export function setEvalGauge(el, score, { pov = SIDE.WHITE, whiteToMove = true } = {}) {
  if (!el) return;
  const white = score && sidePovScore(score, pov === SIDE.WHITE);
  const known = white?.mate != null || white?.cp != null;
  el.classList.toggle(EMPTY_CLASS, !known);
  if (known) el.style.setProperty(SHARE_VAR, `${whiteShare(white, whiteToMove) * 100}%`);
  else el.style.removeProperty(SHARE_VAR);
}

export function setEvalGaugeFlipped(el, flipped) {
  el?.classList.toggle(FLIPPED_CLASS, flipped);
}
