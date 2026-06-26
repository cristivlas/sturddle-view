// Selected-tournament "wall": a pulp fight-bill poster painted on the empty
// Boards region in Studio. Same facts as the Info dialog, reflowed for a wide
// idle canvas -- giant name as hero with stamped placards, a dotted-leader
// setup ledger, the contenders, fine print, and a progress / LLR meter footer.
// Pure: callers own when it shows/hides.

import { progressBarHtml, progressLabelHtml, totalGames } from "./tournament-row.js";
import { STATUS, sprtVerdict } from "./tournament-events.js";
import { escapeHtml } from "./wb-utils.js";
import { formatType, formatResign, formatDraw } from "./tournament-format.js";

// Max engine chips on the wall before collapsing the rest into "+K more".
const ROSTER_CAP = 8;
// Pulp fight-bill fixed copy.
const TAPE_TITLE = "The Setup";
const CONTENDERS_TITLE = "The Contenders";
const VS_TEXT = "vs";
// SPRT placard text + verdict labels parked at the meter ends.
const SPRT_LABEL = "SPRT";
const SPRT_H0_LABEL = "H0";
const SPRT_H1_LABEL = "H1";
const LLR_PREFIX = "LLR ";
// Hero name font ceiling (px): shrinks linearly with name length at
// IW_NAME_PX_PER_CHAR px/char, capped to [IW_NAME_MIN_PX, IW_NAME_MAX_PX].
// Drives the --iw-name-max CSS clamp var.
const IW_NAME_MAX_PX = 96;
const IW_NAME_MIN_PX = 38;
const IW_NAME_PX_PER_CHAR = 1.3;

function nameSizeCeiling(name) {
  const px = IW_NAME_MAX_PX - IW_NAME_PX_PER_CHAR * (name || "").length;
  return Math.round(Math.max(IW_NAME_MIN_PX, Math.min(IW_NAME_MAX_PX, px)));
}

function basename(p) {
  if (!p) return "";
  return String(p).replace(/[\\/]+$/, "").split(/[\\/]/).pop();
}

// Combine resign + draw adjudication into one terse value (or null if neither).
function formatAdjudication(tpl) {
  const parts = [];
  const r = formatResign(tpl.resign);
  const d = formatDraw(tpl.draw);
  if (r) parts.push(`resign ${r}`);
  if (d) parts.push(`draw ${d}`);
  return parts.length ? parts.join("  /  ") : null;
}

function formatCreated(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleString();
}

// One ledger row of the tale-of-the-tape: label on the left, leader dots
// stretch to fill the width, big numeral hard right. Empty values skipped.
function tapeRow(label, value) {
  if (value == null || value === "") return "";
  return `<div class="iw-tape-row">` +
    `<span class="iw-tape-label">${escapeHtml(label)}</span>` +
    `<span class="iw-tape-dots"></span>` +
    `<span class="iw-tape-value">${escapeHtml(String(value))}</span></div>`;
}

// The headline facts as a dotted-leader ledger. Values are the artwork, so a
// short "4" reads as a billed stat, not a half-empty cell. SPRT swaps the
// fixed-rounds rows for the hypothesis bounds it actually runs to.
function taleOfTheTape(t) {
  const tpl = t.template || {};
  const sprt = t.sprt;
  const facts = [
    ["Format", formatType(tpl.tournament_type)],
    ["Time control", tpl.tc],
  ];
  if (!tpl.sprt) {
    facts.push(["Rounds", tpl.rounds]);
  }
  facts.push(["Games in parallel", tpl.games_in_parallel]);
  if (sprt?.pairs > 0) facts.push(["Pairs", sprt.pairs]);
  const rows = facts.map(([l, v]) => tapeRow(l, v)).join("");
  if (!rows) return "";
  return `<div class="iw-tape">` +
    `<div class="iw-tape-title">${TAPE_TITLE}</div>${rows}</div>`;
}

// Fine print: opening book, syzygy, adjudication as the sanctioning-body small
// print at the foot of the bill (only those that are set).
function finePrint(t) {
  const tpl = t.template || {};
  const ed = t.engine_defaults || {};
  const items = [
    ["Book", basename(ed.book_path)],
    ["Syzygy", basename(ed.syzygy_path)],
    ["Adjudication", formatAdjudication(tpl)],
  ].filter(([, v]) => v);
  if (!items.length) return "";
  const html = items.map(([l, v]) =>
    `<span class="iw-fact"><span class="iw-fact-key">${escapeHtml(l)}</span> ${escapeHtml(v)}</span>`
  ).join("");
  return `<div class="iw-setup">${html}</div>`;
}

// One fighter name-plate: name + muted version, on a skewed sticker. Seeds get
// a heavier, highlighted plate so the gauntlet runner stands out.
function fighterChip(e, seed = false) {
  const ver = e.version ? `<span class="iw-engine-ver">${escapeHtml(e.version)}</span>` : "";
  const cls = seed ? "iw-engine iw-engine--seed" : "iw-engine";
  return `<span class="${cls}">${escapeHtml(e.name)}${ver}</span>`;
}

// A capped chip roster + "+K more" overflow tag for an engine list.
function rosterChips(engines) {
  const shown = engines.slice(0, ROSTER_CAP);
  const chips = shown.map((e) => fighterChip(e));
  const extra = engines.length - shown.length;
  if (extra > 0) chips.push(`<span class="iw-engine iw-engine--more">+${extra} more</span>`);
  return chips.join("");
}

// Gauntlet seed count when valid (1..n-1); else 0. Seeds are the leading
// engines and play the whole field.
function seedCount(t) {
  const tpl = t.template || {};
  if (tpl.tournament_type !== "gauntlet") return 0;
  const n = (t.engines || []).length;
  const seeds = Number(tpl.seeds);
  return seeds >= 1 && seeds < n ? seeds : 0;
}

// Wrap a contenders body in the framed card with its nameplate.
function contendersCard(body) {
  return `<div class="iw-card"><div class="iw-tape-title">${CONTENDERS_TITLE}</div>${body}</div>`;
}

// A left vs right billing (each side already rendered HTML).
function versus(left, right) {
  return `<div class="iw-versus">${left}<span class="iw-vs">${VS_TEXT}</span>${right}</div>`;
}

// The contenders. Gauntlets bill the seed(s) against the field; a two-engine
// round-robin is a straight A vs B; anything else is a contender roster.
function contenders(t) {
  const engines = t.engines || [];
  if (!engines.length) return "";
  const seeds = seedCount(t);
  if (seeds) {
    const left = engines.slice(0, seeds).map((e) => fighterChip(e, true)).join("");
    return contendersCard(versus(
      `<span class="iw-roster">${left}</span>`,
      `<span class="iw-roster">${rosterChips(engines.slice(seeds))}</span>`));
  }
  if (engines.length === 2)
    return contendersCard(versus(fighterChip(engines[0]), fighterChip(engines[1])));
  return contendersCard(`<div class="iw-roster">${rosterChips(engines)}</div>`);
}

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
    const tally = total ? `${played} / ${total} games` : (played ? `${played} games` : "");
    lead = `<div class="iw-tally">${escapeHtml(tally)}</div>`;
  }
  const created = formatCreated(t.created_at);
  const stamp = created ? `<div class="iw-created">created ${escapeHtml(created)}</div>` : "";
  return `<div class="iw-footer">${lead}${stamp}</div>`;
}

// Paint (or clear) the wall for a tournament. el is the wall host; t is a
// tournament-shaped object (list row or REST detail), or null to clear.
// Pulp fight-bill: a fat tilted headline with stamped placards (SPRT + status),
// a dotted-leader tale of the tape, the contenders, the sanctioning fine print,
// and a bell-to-bell progress / LLR meter.
export function renderInfoWall(el, t) {
  if (!el) return;
  if (!t) { el.replaceChildren(); return; }
  const status = escapeHtml(t.status || "");
  el.style.setProperty("--iw-name-max", `${nameSizeCeiling(t.name)}px`);
  const sprtLive = t.status === STATUS.RUNNING ? " iw-burst--sprt-live" : "";
  const sprtStamp = t.template?.sprt
    ? `<div class="iw-burst iw-burst--sprt${sprtLive}"><span>${SPRT_LABEL}</span></div>` : "";
  el.innerHTML =
    `<div class="iw-head">` +
      `<h2 class="iw-name">${escapeHtml(t.name || "")}</h2>` +
      `<div class="iw-stamps">${sprtStamp}` +
        `<div class="iw-burst iw-burst--${status}"><span>${status}</span></div></div>` +
    `</div>` +
    `<div class="iw-bill">${taleOfTheTape(t)}${contenders(t)}</div>` +
    `${finePrint(t)}` +
    `${footerHtml(t)}`;
}
