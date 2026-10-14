// Standings table: the engine standings grid (data-table.js) plus the SPRT
// and incomplete-pairs rows. Shared by the Arena workspace and Studio. Pure:
// makeStandingsBody builds the DOM; renderStandings fills it from a
// tournament `detail` payload.

import { SPRT, sprtVerdict } from "./tournament-events.js";
import { MIDDOT, NO_GAMES_MSG } from "./tournament-row.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { SORT_DIR } from "./col-sort.js";
import { createTable } from "./data-table.js";
import { escapeHtml, markSelectable } from "./wb-utils.js";
import { fmtSignedElo, fmtMargin } from "./tournament-format.js";

const STANDINGS_MIN_PCT = 4;

// Ordo-cell tooltip when the fit is anchored to known engine ratings.
const ANCHORED_TITLE = "Absolute Elo, anchored to rated engines";

// Sort sentinel for "--" Elo/Ordo cells: far below any real rating so
// they land last under the descending firstDir.
const MISSING_ELO_SORT = -1e9;

const signedEloCell = (elo, margin) => fmtSignedElo(elo) + fmtMargin(margin);

// Anchored (absolute, unsigned) when any engine carries a known rating;
// mean-centered signed relative otherwise.
function ordoCell(e) {
  if (e.elo_anchored != null) {
    return `<span title="${ANCHORED_TITLE}">${Math.round(e.elo_anchored)}${fmtMargin(e.elo_ordo_margin_95)}</span>`;
  }
  return e.elo_ordo == null ? "--" : signedEloCell(e.elo_ordo, e.elo_ordo_margin_95);
}

// Numeric columns open descending (biggest first); the name column is the
// deterministic tiebreak. Cells read the raw engine (`row.engine`); sorting
// reads the flattened row fields.
const STANDINGS_COLUMNS = [
  { key: "name", label: "Engine", width: 25, alwaysOn: true, firstDir: SORT_DIR.ASC, tiebreak: true,
    tdClass: "wb-eng-name", tdTitle: (r) => r.name, cell: (r) => escapeHtml(r.name) },
  { key: "games", label: "G", width: 7, numeric: true, firstDir: SORT_DIR.DESC, cell: (r) => r.games },
  { key: "wins", label: "W", width: 7, numeric: true, firstDir: SORT_DIR.DESC, cell: (r) => r.wins },
  { key: "losses", label: "L", width: 7, numeric: true, firstDir: SORT_DIR.DESC, cell: (r) => r.losses },
  { key: "draws", label: "D", width: 7, numeric: true, firstDir: SORT_DIR.DESC, cell: (r) => r.draws },
  { key: "points", label: "Pts", width: 7, numeric: true, firstDir: SORT_DIR.DESC, cell: (r) => r.points },
  { key: "pct", label: "%", width: 8, numeric: true, firstDir: SORT_DIR.DESC,
    cell: (r) => `${(r.pct * 100).toFixed(1)}%` },
  { key: "elo", label: "Elo", width: 14, numeric: true, firstDir: SORT_DIR.DESC,
    cell: ({ engine: e }) => (e.elo == null ? "--" : signedEloCell(e.elo, e.elo_margin_95)) },
  { key: "ordo", label: "Ordo", width: 18, numeric: true, firstDir: SORT_DIR.DESC,
    cell: ({ engine: e }) => ordoCell(e) },
];

export function makeStandingsBody() {
  const el = document.createElement("div");
  el.className = "wb-standings";
  el.innerHTML = `
    <div class="wb-sprt-slot"></div>
    <div class="wb-empty wb-standings-empty">Loading...</div>
  `;
  // Shared by every standings surface (Arena + Studio).
  const table = createTable({
    cls: "wb-standings",
    tableClass: "wb-table wb-standings-tbl",
    columns: STANDINGS_COLUMNS,
    layoutKey: STORAGE_KEY.TOURNAMENTS_STANDINGS_LAYOUT,
    minPct: STANDINGS_MIN_PCT,
    traits: {
      sort: {
        sortKey: STORAGE_KEY.TOURNAMENTS_STANDINGS_SORT,
        stackKey: STORAGE_KEY.TOURNAMENTS_STANDINGS_STACK,
        defaultState: { key: "pct", dir: SORT_DIR.DESC },
      },
      resize: true, reorder: true, hide: true, fit: true, rowNav: {},
    },
    rowAttrs: (r) => ({ "data-id": r.name }),
  });
  table.wrap.hidden = true;
  markSelectable(table.headTable);
  markSelectable(table.bodyTable);
  el.append(table.wrap);
  el._table = table;
  return el;
}

// Fill a standings body (from makeStandingsBody) from a tournament detail
// payload. Tolerates null detail (shows the empty state).
export function renderStandings(el, detail, studio = false) {
  const sprtSlot = el.querySelector(".wb-sprt-slot");
  const emptyEl = el.querySelector(".wb-standings-empty");
  const wrapEl = el._table.wrap;
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
  el._table.render(standings.engines.map((e) => ({
    name: e.name, games: e.games, wins: e.wins, losses: e.losses,
    draws: e.draws, points: e.points, pct: e.score_pct,
    elo: e.elo ?? MISSING_ELO_SORT,
    ordo: e.elo_anchored ?? e.elo_ordo ?? MISSING_ELO_SORT,
    engine: e,
  })));
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
