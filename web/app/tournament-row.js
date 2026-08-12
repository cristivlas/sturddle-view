// Shared tournament list-row renderer + reusable cell snippets. Arena's list
// and Studio (list + table) all render the same status/SPRT badges and
// running progress bar, so those are exported as small HTML helpers. Pure --
// callers wire selection/click behavior via options.

import { STATUS } from "./tournament-events.js";
import { escapeHtml } from "./wb-utils.js";

// Shared status-strip separator (the tournament progress label, the SPRT
// line, the H2H banner). Exported so those strips read consistently.
export const MIDDOT = "\u00B7";

// Empty-state message shared by the standings and H2H surfaces.
export const NO_GAMES_MSG = "No completed games yet.";

// Live-row glyphs (engine list, games-in-play list), shared by the Arena
// workspace and Studio. The trailing U+FE0E is variation selector-15, which
// pins text presentation: without it iOS renders U+2699 from the color-emoji
// face, which ignores CSS color and shows grey instead of the accent tint.
// Desktop already picks the text face, so the selector is a no-op there.
export const ICON_ENGINE_ROW = "&#9881;&#xFE0E;";
export const ICON_GAME_ROW = "&#9822;&#xFE0E;";

// Engine-row state: with 3+ engines some sit idle between games; badge and
// dim those rows so the watcher can tell who is actually playing. Arena and
// Studio both render engine rows, so the treatment lives here rather than
// being wired twice.
const ENGINE_IDLE_CLASS = "wb-engine-idle";

// Appends the state badge to an engine row (after its existing content) and
// dims the row when idle. Call once per row, before the watch controls.
export function markEngineRow(li, playing) {
  li.classList.toggle(ENGINE_IDLE_CLASS, !playing);
  li.insertAdjacentHTML(
    "beforeend",
    `<span class="wb-engine-state">${playing ? "playing" : "idle"}</span>`,
  );
}

// Total scheduled games for a tournament, or null when the template is
// underspecified. Gauntlet seeds play every non-seed; round-robin pairs
// every engine once per (rounds * games_per_round).
export function totalGames(t) {
  const tpl = t.template || {};
  // SPRT self-terminates -- no fixed total even though rounds is stored.
  if (tpl.sprt) return null;
  const n = (t.engines || []).length;
  const rounds = Number(tpl.rounds);
  const gpr = Number(tpl.games_per_round ?? 2);
  if (!n || !rounds || !gpr) return null;
  if (tpl.tournament_type === "gauntlet") {
    const seeds = Number(tpl.seeds);
    if (!seeds || seeds >= n) return null;
    return seeds * (n - seeds) * rounds * gpr;
  }
  const pairings = (n * (n - 1)) / 2;
  return pairings * rounds * gpr;
}

function pctOf(played, total) {
  return total ? Math.min(100, Math.round((played / total) * 100)) : 0;
}

// ---- Reusable cell snippets (shared by Arena and Studio renderers) -------

export function statusBadgeHtml(status) {
  const s = escapeHtml(status);
  return `<span class="tournament-status status-${s}">${s}</span>`;
}

// SPRT marker as a status-style pill (shares .tournament-status rendering),
// sat alongside the status badge.
export function sprtBadgeHtml(t) {
  return t.template?.sprt
    ? `<span class="tournament-status tournament-status--sprt">SPRT</span>` : "";
}

export function progressBarHtml(played, total) {
  return `<div class="tournament-progress" role="progressbar" aria-valuemin="0"` +
    ` aria-valuemax="${total}" aria-valuenow="${played}">` +
    `<div class="tournament-progress-fill" style="width: ${pctOf(played, total)}%"></div></div>`;
}

function progressLabelText(played, total) {
  return `${played} / ${total} ${MIDDOT} ${pctOf(played, total)}%`;
}

export function progressLabelHtml(played, total) {
  return `<span class="tournament-progress-label">${progressLabelText(played, total)}</span>`;
}

// Build the <li> for one tournament. options: { selected, onSelect(t),
// onInfo(t) }. onSelect fires on click, onInfo on double-click.
export function renderTournamentRow(t, { selected = false, onSelect, onInfo } = {}) {
  const li = document.createElement("li");
  li.className = "tournament-row" + (selected ? " selected" : "");
  li.dataset.id = t.id;

  const isRunning = t.status === STATUS.RUNNING;
  const played = t.standings?.games ?? 0;
  const total = totalGames(t);
  const trailing = (isRunning && total)
    ? progressBarHtml(played, total) + progressLabelHtml(played, total)
    : `<span class="tournament-engines muted"></span>`;

  li.innerHTML = `
    <div class="tournament-row-main">
      ${statusBadgeHtml(t.status)}
      ${sprtBadgeHtml(t)}
      <span class="tournament-name"></span>
      ${trailing}
    </div>
  `;

  li.querySelector(".tournament-name").textContent = t.name;
  if (!isRunning || !total) {
    const engineNames = (t.engines || []).map((e) => e.name).join(", ");
    li.querySelector(".tournament-engines").textContent = engineNames;
  }

  if (onSelect) li.addEventListener("click", () => onSelect(t));
  if (onInfo) li.addEventListener("dblclick", () => onInfo(t));
  return li;
}

// Update an already-rendered row's progress bar in place (live game ticks).
// No-op unless the row is in its running/progress layout.
export function updateRowProgress(row, t) {
  const played = t?.standings?.games;
  if (played == null || !row) return;
  const bar = row.querySelector(".tournament-progress");
  const fill = row.querySelector(".tournament-progress-fill");
  const label = row.querySelector(".tournament-progress-label");
  if (!bar || !fill || !label) return;
  const total = totalGames(t);
  if (!total) return;
  bar.setAttribute("aria-valuenow", String(played));
  fill.style.width = `${pctOf(played, total)}%`;
  label.textContent = progressLabelText(played, total);
}
