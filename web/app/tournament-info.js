// Selected-tournament "wall": a typographic poster painted on the empty
// Boards region in Studio. Same facts as the Info dialog, but reflowed for a
// wide, idle canvas -- giant name as hero, uppercase stat labels with large
// values, hairline rules, an engine roster, and a footer (live progress or
// games + created). Pure: callers own when it shows/hides.

import { progressBarHtml, progressLabelHtml, sprtBadgeHtml, totalGames } from "./tournament-row.js";
import { STATUS } from "./tournament-events.js";
import { escapeHtml } from "./wb-utils.js";

const TYPE_LABEL = Object.freeze({ roundrobin: "Round-robin", gauntlet: "Gauntlet" });
const UNLIMITED_SPRT = "unlimited (SPRT)";
// Max engine chips on the wall before collapsing the rest into "+K more".
const ROSTER_CAP = 8;

function formatType(v) {
  if (!v) return null;
  return TYPE_LABEL[v] || (v.charAt(0).toUpperCase() + v.slice(1));
}

function basename(p) {
  if (!p) return "";
  return String(p).replace(/[\\/]+$/, "").split(/[\\/]/).pop();
}

function formatResign(r) {
  if (!r || r.movecount == null || r.score == null) return null;
  return `${r.movecount} moves @ ${r.score}cp`;
}

function formatDraw(d) {
  if (!d || d.movenumber == null) return null;
  return `move ${d.movenumber}, ${d.movecount} @ ${d.score}cp`;
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

// One comic "panel": label flag + big value, thick-bordered cell. Skipped
// when the value is empty so the strip stays dense.
function statPanel(label, value) {
  if (value == null || value === "") return "";
  return `<div class="iw-panel">` +
    `<div class="iw-panel-label">${escapeHtml(label)}</div>` +
    `<div class="iw-panel-value">${escapeHtml(String(value))}</div></div>`;
}

// The panel strip: the four headline facts. Book/syzygy/adjudication move to
// the quieter setup line so the grid stays uncluttered.
function statStrip(t) {
  const tpl = t.template || {};
  const schedule = tpl.sprt ? UNLIMITED_SPRT : (tpl.rounds != null ? `${tpl.rounds} rounds` : "");
  return [
    ["Type", formatType(tpl.tournament_type)],
    ["Time control", tpl.tc],
    ["Schedule", schedule],
    ["Parallel", tpl.games_in_parallel],
  ].map(([l, v]) => statPanel(l, v)).join("");
}

// Quiet setup line: opening book, syzygy, adjudication as muted label:value
// pairs (only those that are set). Secondary detail, no heavy panels.
function setupLine(t) {
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

// Engine roster as speech-bubble-ish tags (name + muted version). Capped so a
// large field can't balloon the splash; the overflow is summed in a final
// "+K more" chip (the full list lives in the Info dialog).
function rosterHtml(t) {
  const engines = t.engines || [];
  const shown = engines.slice(0, ROSTER_CAP);
  const chips = shown.map((e) => {
    const ver = e.version ? `<span class="iw-engine-ver">${escapeHtml(e.version)}</span>` : "";
    return `<span class="iw-engine">${escapeHtml(e.name)}${ver}</span>`;
  });
  const extra = engines.length - shown.length;
  if (extra > 0) chips.push(`<span class="iw-engine iw-engine--more">+${extra} more</span>`);
  return chips.length ? `<div class="iw-roster">${chips.join("")}</div>` : "";
}

// Footer: live progress bar while running with a known total; otherwise a
// games tally. Created stamp sits opposite, muted.
function footerHtml(t) {
  const played = t.standings?.games ?? 0;
  const total = totalGames(t);
  let lead;
  if (t.status === STATUS.RUNNING && total) {
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
// Comic-book treatment: a slanted status starburst, a fat tilted headline,
// thick-bordered stat panels, and tag-style engines.
export function renderInfoWall(el, t) {
  if (!el) return;
  if (!t) { el.replaceChildren(); return; }
  const status = escapeHtml(t.status || "");
  el.innerHTML =
    `<div class="iw-head">` +
      `<h2 class="iw-name">${escapeHtml(t.name || "")}${sprtBadgeHtml(t)}</h2>` +
      `<div class="iw-burst iw-burst--${status}"><span>${status}</span></div>` +
    `</div>` +
    `<div class="iw-stats">${statStrip(t)}</div>` +
    `${setupLine(t)}` +
    `${rosterHtml(t)}` +
    `${footerHtml(t)}`;
}
