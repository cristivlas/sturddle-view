// Head-to-head detail: a Studio-only view of per-engine white/black splits and
// LOS, computed client-side from the tournament detail (no backend call). The
// shared Standings table stays untouched; this is the "more detail" surface.
//
// Split like the standings table: makeH2HBody builds the DOM once (a
// data-table.js table); renderH2H fills the banner + rows from a tournament
// `detail` payload (standings engines + the per-game {white, black, result}
// list, both already in hand). The split keeps resize
// listeners from leaking on each live repaint.

import { RESULT } from "./chess-consts.js";
import { MIDDOT, NO_GAMES_MSG } from "./tournament-row.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { createTable } from "./data-table.js";
import { escapeHtml, markSelectable } from "./wb-utils.js";
import { fmtSignedElo, fmtMargin } from "./tournament-format.js";

const NA = "--";
// Banner segment separator, matching the SPRT line and progress label.
const SEP = ` ${MIDDOT} `;
// Resize floor (default widths live on H2H_COLUMNS).
const H2H_MIN_PCT = 6;
const GROUP_ROW_CLASS = "h2h-group";

// Logistic Elo from a 0..1 score; null for a perfect 0/1 (Elo undefined).
function eloFromScore(score) {
  if (score <= 0 || score >= 1) return null;
  return -400 * Math.log10(1 / score - 1);
}

// 95% Elo half-width from a W/L/D record (mirrors the server's
// elo_margin_from_wld: Bessel sample variance propagated to Elo). null when
// n<2 or the score is 0/1 (CI undefined).
function eloMargin(wins, losses, draws) {
  const n = wins + losses + draws;
  if (n < 2) return null;
  const s = (wins + 0.5 * draws) / n;
  if (s <= 0 || s >= 1) return null;
  const variance =
    (wins * (1 - s) ** 2 + losses * s ** 2 + draws * (0.5 - s) ** 2) / (n - 1);
  if (variance <= 0) return 0;
  const seScore = Math.sqrt(variance / n);
  const dEloDScore = 400 / (Math.LN10 * s * (1 - s));
  return 1.96 * seScore * dEloDScore;
}

// Abramowitz-Stegun 7.1.26 erf approximation (~1e-7); no Math.erf in JS.
function erf(x) {
  const sign = x < 0 ? -1 : 1;
  const ax = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * ax);
  const y =
    1 -
    ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) *
      t +
      0.254829592) *
      t *
      Math.exp(-ax * ax);
  return sign * y;
}

// Standard normal CDF.
function phi(x) {
  return 0.5 * (1 + erf(x / Math.SQRT2));
}

// Likelihood of superiority: P(true Elo > 0). Derived from the *same* SE that
// produces the CI, so LOS and the +/- margin always agree. A perfect score is
// 100%/0% certainty; n<2 leaves it undefined.
function los(wins, losses, draws) {
  const elo = eloFromScore((wins + 0.5 * draws) / (wins + losses + draws || 1));
  if (elo === null) {
    if (wins + losses + draws === 0) return null;
    return wins > losses ? 100 : 0;
  }
  const margin = eloMargin(wins, losses, draws);
  if (margin == null || margin <= 0) return null;
  return phi(elo / (margin / 1.96)) * 100;
}

// Tally a single engine's W/L/D from the engine's own POV, split by color.
function colorSplit(games, name) {
  const white = { wins: 0, losses: 0, draws: 0 };
  const black = { wins: 0, losses: 0, draws: 0 };
  for (const g of games) {
    if (g.white === name) {
      if (g.result === RESULT.WHITE_WIN) white.wins++;
      else if (g.result === RESULT.BLACK_WIN) white.losses++;
      else white.draws++;
    } else if (g.black === name) {
      if (g.result === RESULT.BLACK_WIN) black.wins++;
      else if (g.result === RESULT.WHITE_WIN) black.losses++;
      else black.draws++;
    }
  }
  return { white, black };
}

function fmtPts(pts) {
  return Number.isInteger(pts) ? String(pts) : pts.toFixed(1);
}

// Elo +/- margin from a W/L/D record (computed locally for per-color splits).
function fmtElo(wins, losses, draws) {
  const elo = eloFromScore((wins + 0.5 * draws) / (wins + losses + draws || 1));
  if (elo === null) return NA;
  return fmtSignedElo(elo) + fmtMargin(eloMargin(wins, losses, draws));
}

// Elo cell straight from a standings row (keeps the banner in lockstep with the
// Standings tab's number for the same engine).
function fmtRowElo(e) {
  if (e.elo == null) return NA;
  return fmtSignedElo(e.elo) + fmtMargin(e.elo_margin_95);
}

function fmtLos(wins, losses, draws) {
  const v = los(wins, losses, draws);
  return v == null ? NA : `${v.toFixed(1)}%`;
}

const statN = (r) => r.wins + r.losses + r.draws;
const statPts = (r) => r.wins + 0.5 * r.draws;

// Detail-line columns: side label, score, percent, W-D-L, and (color rows
// only, `withElo`) Elo + LOS. The Overall row leaves the Elo/LOS cells blank.
const H2H_COLUMNS = [
  { key: "side", label: "", width: 18, tdClass: "h2h-side", cell: (r) => r.side },
  { key: "score", label: "Score", width: 16, cell: (r) => `${fmtPts(statPts(r))}/${statN(r)}` },
  { key: "pct", label: "%", width: 12,
    cell: (r) => `${(statN(r) ? (statPts(r) / statN(r)) * 100 : 0).toFixed(1)}%` },
  { key: "wld", label: "W-D-L", width: 20, tdClass: "h2h-wld", cell: (r) => `+${r.wins} =${r.draws} -${r.losses}` },
  { key: "elo", label: "Elo", width: 20, cell: (r) => (r.withElo ? fmtElo(r.wins, r.losses, r.draws) : "") },
  { key: "los", label: "LOS", width: 14, cell: (r) => (r.withElo ? fmtLos(r.wins, r.losses, r.draws) : "") },
];

// A per-engine group: a name group row (full width) then Overall / White /
// Black. Overall is the white+black sum (single source -- the games list), so
// the three rows are always self-consistent regardless of the standings W/L/D.
function engineRows(e, games) {
  const { white, black } = colorSplit(games, e.name);
  const overall = {
    wins: white.wins + black.wins, losses: white.losses + black.losses, draws: white.draws + black.draws,
  };
  return [
    { group: e.name },
    { engine: e.name, side: "Overall", ...overall, withElo: false },
    { engine: e.name, side: "White", wins: white.wins, losses: white.losses, draws: white.draws, withElo: true },
    { engine: e.name, side: "Black", wins: black.wins, losses: black.losses, draws: black.draws, withElo: true },
  ];
}

// 2-engine headline: leader (engines[0], sorted by points) vs the other, with
// the head-to-head Elo and LOS.
function bannerHtml(engines) {
  const [a, b] = engines;
  return (
    `<div class="wb-sprt h2h-banner">` +
    `${escapeHtml(a.name)} vs ${escapeHtml(b.name)}${SEP}` +
    `${fmtRowElo(a)} Elo${SEP}LOS ${fmtLos(a.wins, a.losses, a.draws)}` +
    `</div>`
  );
}

// Build the H2H body once: a banner slot, an empty-state line, and a
// resize-only table (no sort, no columns menu; arrow-key row cursor) the
// fill step populates.
// Mirrors makeStandingsBody.
export function makeH2HBody() {
  const el = document.createElement("div");
  el.className = "wb-h2h";
  el.innerHTML = `
    <div class="h2h-banner-slot"></div>
    <div class="wb-empty h2h-empty">${NO_GAMES_MSG}</div>`;
  const table = createTable({
    cls: "h2h",
    tableClass: "wb-table h2h-tbl",
    columns: H2H_COLUMNS,
    layoutKey: STORAGE_KEY.STUDIO_H2H_LAYOUT,
    minPct: H2H_MIN_PCT,
    traits: { resize: true, rowNav: {} },
    // Group rows carry no data-id, so the row cursor skips them.
    rowAttrs: (r) => (r.group != null ? { class: GROUP_ROW_CLASS } : { "data-id": `${r.engine}:${r.side}` }),
    fullRow: (r) => (r.group != null ? { html: escapeHtml(r.group) } : null),
  });
  table.wrap.hidden = true;
  markSelectable(table.headTable);
  markSelectable(table.bodyTable);
  el.append(table.wrap);
  el._table = table;
  return el;
}

// Fill an H2H body (from makeH2HBody) from a tournament detail payload.
// Tolerates null/empty detail (shows the empty state).
export function renderH2H(el, detail) {
  if (!el) return;
  const bannerSlot = el.querySelector(".h2h-banner-slot");
  const emptyEl = el.querySelector(".h2h-empty");
  const wrapEl = el._table.wrap;
  const engines = detail?.standings?.engines || [];
  const games = detail?.games || [];
  if (engines.length === 0 || games.length === 0) {
    emptyEl.hidden = false;
    wrapEl.hidden = true;
    bannerSlot.innerHTML = "";
    return;
  }
  emptyEl.hidden = true;
  wrapEl.hidden = false;
  // Banner only for a configured 2-engine match -- key off the roster, not the
  // played-so-far standings (a 3-engine RR is transiently 2-engine early on).
  const isPair = detail.engines?.length === 2 && engines.length === 2;
  bannerSlot.innerHTML = isPair ? bannerHtml(engines) : "";
  el._table.render(engines.flatMap((e) => engineRows(e, games)));
}
