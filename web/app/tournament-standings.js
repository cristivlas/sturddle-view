// Standings table: a resizable-column engine standings grid plus the SPRT
// and incomplete-pairs rows. Shared by the Arena workspace and Studio. Pure:
// makeStandingsBody builds the DOM (and wires column resize); renderStandings
// fills it from a tournament `detail` payload.

import { SPRT, STATUS } from "./tournament-events.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { attachColumnResize } from "./col-resize.js";
import { escapeHtml } from "./wb-utils.js";

const STANDINGS_COL_PCTS_KEY = STORAGE_KEY.TOURNAMENTS_STANDINGS_COL_PCTS;
const STANDINGS_DEFAULT_PCTS = [25, 7, 7, 7, 7, 7, 8, 14];
const STANDINGS_MIN_PCT = 4;

export function makeStandingsBody() {
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
        if (a < STANDINGS_MIN_PCT) { b -= STANDINGS_MIN_PCT - a; a = STANDINGS_MIN_PCT; }
        if (b < STANDINGS_MIN_PCT) { a -= STANDINGS_MIN_PCT - b; b = STANDINGS_MIN_PCT; }
        sizes[gripIdx] = a;
        sizes[gripIdx + 1] = b;
      }
      colEls.forEach((c, i) => { c.style.width = sizes[i] + "%"; });
    },
  });
  return el;
}

// Fill a standings body (from makeStandingsBody) from a tournament detail
// payload. Tolerates null detail (shows the empty state).
export function renderStandings(el, detail) {
  const sprtSlot = el.querySelector(".wb-sprt-slot");
  const partialSlot = el.querySelector(".wb-partial-slot");
  const emptyEl = el.querySelector(".wb-standings-empty");
  const wrapEl = el.querySelector(".wb-standings-table-wrap");
  const tbody = el.querySelector(".wb-standings-tbl tbody");
  const standings = detail?.standings;
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
  const sprt = detail.sprt;
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
    const candidate = detail.engines?.[0]?.name ? escapeHtml(detail.engines[0].name) : "candidate";
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
  const partialPairs = detail.partial_pairs ?? 0;
  // Hide during RUNNING -- a fresh game-1 always sits alone in the PGN until
  // game-2 of the pair finishes; that's normal, not data loss.
  const showPartial = partialPairs > 0 && detail.status !== STATUS.RUNNING;
  partialSlot.innerHTML = showPartial
    ? `<div class="wb-partial-pairs">${partialPairs} incomplete pair${partialPairs === 1 ? "" : "s"} ` +
      `(one game missing)</div>`
    : "";
}
