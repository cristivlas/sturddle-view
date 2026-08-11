// Standings table: a resizable-column engine standings grid plus the SPRT
// and incomplete-pairs rows. Shared by the Arena workspace and Studio. Pure:
// makeStandingsBody builds the DOM (and wires column resize); renderStandings
// fills it from a tournament `detail` payload.

import { SPRT, sprtVerdict } from "./tournament-events.js";
import { MIDDOT, NO_GAMES_MSG } from "./tournament-row.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { attachColumnResize, makePctApplySizes } from "./col-resize.js";
import { SORT_DIR } from "./col-sort.js";
import { attachLayeredSort, sortByStack } from "./sort-stack.js";
import { escapeHtml, markSelectable } from "./wb-utils.js";
import { fmtSignedElo, fmtMargin } from "./tournament-format.js";

const STANDINGS_COL_PCTS_KEY = STORAGE_KEY.TOURNAMENTS_STANDINGS_COL_PCTS;
const STANDINGS_DEFAULT_PCTS = [25, 7, 7, 7, 7, 7, 8, 14];
const STANDINGS_MIN_PCT = 4;

// Ordo-cell tooltip when the fit is anchored to known engine ratings.
const ANCHORED_TITLE = "Absolute Elo, anchored to rated engines";

// Sort sentinel for "--" Elo/Ordo cells: far below any real rating so
// they land last under the descending firstDir.
const MISSING_ELO_SORT = -1e9;

// Position-mapped to the <thead> ths. Numeric columns open descending
// (biggest first); the name column is the deterministic tiebreak.
const STANDINGS_SORT_COLS = [
  { key: "name", firstDir: SORT_DIR.ASC, tiebreak: true },
  { key: "games", numeric: true, firstDir: SORT_DIR.DESC },
  { key: "wins", numeric: true, firstDir: SORT_DIR.DESC },
  { key: "losses", numeric: true, firstDir: SORT_DIR.DESC },
  { key: "draws", numeric: true, firstDir: SORT_DIR.DESC },
  { key: "points", numeric: true, firstDir: SORT_DIR.DESC },
  { key: "pct", numeric: true, firstDir: SORT_DIR.DESC },
  { key: "elo", numeric: true, firstDir: SORT_DIR.DESC },
  { key: "ordo", numeric: true, firstDir: SORT_DIR.DESC },
];

export function makeStandingsBody() {
  const el = document.createElement("div");
  el.className = "wb-standings";
  el.innerHTML = `
    <div class="wb-sprt-slot"></div>
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
  markSelectable(tableEl);
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
    applySizes: makePctApplySizes(colEls, STANDINGS_MIN_PCT),
  });
  // Layered header sort, shared by every standings surface (Arena + Studio).
  // The widget re-renders itself from the detail stashed by renderStandings.
  el._sortStack = attachLayeredSort({
    table: tableEl,
    columns: STANDINGS_SORT_COLS,
    sortKey: STORAGE_KEY.TOURNAMENTS_STANDINGS_SORT,
    stackKey: STORAGE_KEY.TOURNAMENTS_STANDINGS_STACK,
    defaultState: { key: "pct", dir: SORT_DIR.DESC },
    // No detail yet = the default-seeding onChange that fires mid-attach,
    // before el._sortStack is assigned; nothing to render then anyway.
    onChange: () => { if (el._detail) renderStandings(el, el._detail, el._studio); },
  }).get;
  return el;
}

// Fill a standings body (from makeStandingsBody) from a tournament detail
// payload. Tolerates null detail (shows the empty state).
export function renderStandings(el, detail, studio = false) {
  // Stashed so the sort-change callback can re-render without a data refetch.
  el._detail = detail;
  el._studio = studio;
  const sprtSlot = el.querySelector(".wb-sprt-slot");
  const emptyEl = el.querySelector(".wb-standings-empty");
  const wrapEl = el.querySelector(".wb-standings-table-wrap");
  const tbody = el.querySelector(".wb-standings-tbl tbody");
  const standings = detail?.standings;
  if (!standings || standings.engines.length === 0) {
    emptyEl.textContent = NO_GAMES_MSG;
    emptyEl.hidden = false;
    wrapEl.hidden = true;
    sprtSlot.innerHTML = "";
    return;
  }
  emptyEl.hidden = true;
  wrapEl.hidden = false;
  const sprt = detail.sprt;
  // Sortable view of the engine list; an empty stack keeps the server order.
  const rows = standings.engines.map((e) => ({
    name: e.name, games: e.games, wins: e.wins, losses: e.losses,
    draws: e.draws, points: e.points, pct: e.score_pct,
    elo: e.elo ?? MISSING_ELO_SORT,
    ordo: e.elo_anchored ?? e.elo_ordo ?? MISSING_ELO_SORT,
    engine: e,
  }));
  sortByStack(rows, el._sortStack(), STANDINGS_SORT_COLS);
  tbody.innerHTML = rows
    .map(({ engine: e }) => {
      const eloCell = e.elo == null
        ? "--"
        : fmtSignedElo(e.elo) + fmtMargin(e.elo_margin_95);
      // Anchored (absolute, unsigned) when any engine carries a known
      // rating; mean-centered signed relative otherwise.
      const ordoCell = e.elo_anchored != null
        ? `<span title="${ANCHORED_TITLE}">${Math.round(e.elo_anchored)}${fmtMargin(e.elo_ordo_margin_95)}</span>`
        : e.elo_ordo == null
          ? "--"
          : fmtSignedElo(e.elo_ordo) + fmtMargin(e.elo_ordo_margin_95);
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
    const { lo, hi, llr, concluded, isH1 } = sprtVerdict(sprt);
    const colorMod = concluded ? (isH1 ? " wb-sprt--h1" : " wb-sprt--h0") : "";
    const studioMod = studio ? " wb-sprt--studio" : "";
    const candidate = detail.engines?.[0]?.name ? escapeHtml(detail.engines[0].name) : "candidate";
    const pairsText = sprt.pairs != null ? ` ${MIDDOT} ${sprt.pairs} pair${sprt.pairs === 1 ? "" : "s"}` : "";
    const statusText = sprt.status === SPRT.H1
      ? ` ${MIDDOT} H1`
      : sprt.status === SPRT.H0
        ? ` ${MIDDOT} H0`
        : "";
    sprtSlot.innerHTML = `<div class="wb-sprt${colorMod}${studioMod}">` +
      `SPRT ${candidate} [${sprt.elo0}, ${sprt.elo1}] ${MIDDOT} LLR=${llr.toFixed(2)} [${lo.toFixed(2)}, ${hi.toFixed(2)}]` +
      `${pairsText}${statusText}` +
      `</div>`;
  } else {
    sprtSlot.innerHTML = "";
  }
}
