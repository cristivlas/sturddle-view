// Selected-tournament "wall" on the Boards region in Studio: a progress / LLR
// meter footer over the (optional) wallpaper. Pure: callers own when it
// shows/hides.

import { progressBarHtml, progressLabelHtml, totalGames } from "./tournament-row.js";
import { STATUS, sprtVerdict } from "./tournament-events.js";
import { escapeHtml } from "./wb-utils.js";
import { formatStamp } from "./tournament-format.js";

// Verdict labels parked at the SPRT meter ends.
const SPRT_H0_LABEL = "H0";
const SPRT_H1_LABEL = "H1";
const LLR_PREFIX = "LLR ";

// SPRT verdict meter: the LLR marker walking between the H0/H1 bounds -- the
// real "how close to a conclusion" gauge, since SPRT has no fixed finish line.
// Returns "" when bounds/llr are absent (e.g. a list row without live data).
function llrMeterHtml(sprt) {
  const { lo, hi, llr, concluded, isH1 } = sprtVerdict(sprt);
  if (lo == null || hi == null || llr == null || hi <= lo) return "";
  const frac = Math.max(0, Math.min(1, (llr - lo) / (hi - lo)));
  const mod = concluded ? (isH1 ? " iw-llr--h1" : " iw-llr--h0") : "";
  return `<div class="iw-llr${mod}">` +
    `<span class="iw-llr-end">${SPRT_H0_LABEL}</span>` +
    `<span class="iw-llr-track"><span class="iw-llr-mark" style="left:${(frac * 100).toFixed(1)}%"></span></span>` +
    `<span class="iw-llr-end">${SPRT_H1_LABEL}</span>` +
    `<span class="iw-llr-val">${LLR_PREFIX}${llr.toFixed(2)}</span></div>`;
}

// Footer: SPRT LLR meter when available, else a live progress bar with a known
// total, else a games tally. Created stamp sits opposite, muted.
function footerHtml(t) {
  const played = t.standings?.games ?? 0;
  const total = totalGames(t);
  const meter = t.sprt ? llrMeterHtml(t.sprt) : "";
  let lead;
  if (meter) {
    lead = meter;
  } else if (t.status === STATUS.RUNNING && total) {
    lead = `<div class="iw-progress">${progressLabelHtml(played, total)}${progressBarHtml(played, total)}</div>`;
  } else {
    const tally = total
      ? `${played} / ${total} game${total === 1 ? "" : "s"}`
      : (played ? `${played} game${played === 1 ? "" : "s"}` : "");
    lead = `<div class="iw-tally">${escapeHtml(tally)}</div>`;
  }
  const created = formatStamp(t.created_at);
  const stamp = created ? `<div class="iw-created">created ${escapeHtml(created)}</div>` : "";
  return `<div class="iw-footer">${lead}${stamp}</div>`;
}

// Paint (or clear) the wall for a tournament. el is the wall host; t is a
// tournament-shaped object (list row or REST detail), or null to clear.
export function renderInfoWall(el, t) {
  if (!el) return;
  if (!t) { el.replaceChildren(); return; }
  el.innerHTML = footerHtml(t);
}
