// Shared tournament list-row renderer. Arena's list and Studio's list +
// selected-tourney header all render the same row: status badge, name,
// optional SPRT badge, and a running progress bar (or engine names when
// not running). Pure -- callers wire selection/click behavior via options.

import { STATUS } from "./tournament-events.js";

// Total scheduled games for a tournament, or null when the template is
// underspecified. Gauntlet seeds play every non-seed; round-robin pairs
// every engine once per (rounds * games_per_round).
export function totalGames(t) {
  const tpl = t.template || {};
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

// Build the <li> for one tournament. options: { selected, onSelect(t),
// onInfo(t) }. onSelect fires on click, onInfo on double-click.
export function renderTournamentRow(t, { selected = false, onSelect, onInfo } = {}) {
  const li = document.createElement("li");
  li.className = "tournament-row" + (selected ? " selected" : "");
  li.dataset.id = t.id;

  const status = t.status;
  const isRunning = status === STATUS.RUNNING;
  const played = t.standings?.games ?? 0;
  const total = totalGames(t);
  const pct = total ? Math.min(100, Math.round((played / total) * 100)) : 0;

  let trailing;
  if (isRunning && total) {
    trailing = `
      <div class="tournament-progress" role="progressbar"
           aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${played}">
        <div class="tournament-progress-fill" style="width: ${pct}%"></div>
      </div>
      <span class="tournament-progress-label">${played} / ${total} · ${pct}%</span>
    `;
  } else {
    trailing = `<span class="tournament-engines muted"></span>`;
  }

  const sprtBadge = t.template?.sprt ? `<span class="tournament-sprt-badge">SPRT</span>` : "";
  li.innerHTML = `
    <div class="tournament-row-main">
      <span class="tournament-status status-${status}">${status}</span>
      <span class="tournament-name"></span>
      ${sprtBadge}
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
  const pct = Math.min(100, Math.round((played / total) * 100));
  bar.setAttribute("aria-valuenow", String(played));
  fill.style.width = `${pct}%`;
  label.textContent = `${played} / ${total} · ${pct}%`;
}
