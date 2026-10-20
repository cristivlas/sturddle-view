// Crosstable: each engine's score against every other, one cell per pairing.
// Shaped like the standings table so Studio can mount it too: makeCrosstableBody
// builds the DOM once; renderCrosstable fills it from a tournament `detail`
// payload (standings engines + the per-game {white, black, result} list).

import { LOADING_MSG, NO_GAMES_MSG } from "./tournament-row.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { createTable } from "./data-table.js";
import { SORT_DIR } from "./col-sort.js";
import { escapeHtml, markSelectable } from "./wb-utils.js";
import { colorSplit, fmtPts, fmtWdl, statPts, sumSplit } from "./tournament-h2h.js";
import { ordoCell } from "./tournament-standings.js";

const XT_MIN_PCT = 2;
// Default column weights (relative; the factory scales them to 100%).
const NAME_WIDTH = 24;
const SCORE_WIDTH = 6;
const GAMES_WIDTH = 6;
const PTS_WIDTH = 7;
const ORDO_WIDTH = 16;
// Column count, read by the CSS min-width so a big field scrolls, not squeezes.
const COLS_VAR = "--xt-cols";

// Ordo purges all-wins / all-losses engines from the fit (no rating), so
// they bracket the rated engines instead of sinking to the bottom.
const RANK_GROUP = Object.freeze({ ALL_WINS: 0, RATED: 1, OTHER: 2 });

function rankGroup(e) {
  if (e.elo_ordo != null) return RANK_GROUP.RATED;
  return e.wins > 0 && e.wins === e.games ? RANK_GROUP.ALL_WINS : RANK_GROUP.OTHER;
}

// Ordo (anchored is a constant shift -- same order), then points, then name.
function rankEngines(engines) {
  return [...engines].sort((a, b) =>
    rankGroup(a) - rankGroup(b) ||
    (b.elo_ordo ?? 0) - (a.elo_ordo ?? 0) ||
    b.points - a.points ||
    a.name.localeCompare(b.name));
}

const pairKey = (a, b) => JSON.stringify([a, b].sort());

function gamesByPair(games) {
  const byPair = new Map();
  for (const g of games) {
    const k = pairKey(g.white, g.black);
    if (!byPair.has(k)) byPair.set(k, []);
    byPair.get(k).push(g);
  }
  return byPair;
}

// Row engine's score vs the column engine, with W-D-L by color as the tooltip.
// Blank on the diagonal and for pairs that never met (gauntlet opponents).
function scoreCell(row, col, byPair) {
  const pairGames = row === col ? null : byPair.get(pairKey(row.name, col.name));
  if (!pairGames) return { score: "", tip: "" };
  const split = colorSplit(pairGames, row.name);
  const total = sumSplit(split);
  return {
    score: fmtPts(statPts(total)),
    tip: `${row.name} vs ${col.name}: ${fmtWdl(total)}\n` +
      `White ${fmtWdl(split.white)}\nBlack ${fmtWdl(split.black)}`,
  };
}

// One column per engine (headed by rank), between Engine and the optional
// G / Pts / Ordo. Rank-keyed, so saved widths carry across tournaments.
function crosstableColumns(n) {
  const scoreCols = Array.from({ length: n }, (_, j) => ({
    key: `r${j + 1}`, label: String(j + 1), width: SCORE_WIDTH, alwaysOn: true, sortable: false,
    tdTitle: (r) => r.cells[j].tip, cell: (r) => r.cells[j].score,
  }));
  return [
    { key: "name", label: "Engine", width: NAME_WIDTH, alwaysOn: true, sortable: false, tdClass: "xt-name",
      tdTitle: (r) => r.engine.name,
      cell: (r) => `<span class="xt-rank">${r.rank}</span>${escapeHtml(r.engine.name)}` },
    ...scoreCols,
    { key: "games", label: "G", width: GAMES_WIDTH, numeric: true, firstDir: SORT_DIR.DESC, field: "games",
      defaultHidden: true, cell: (r) => r.engine.games },
    { key: "pts", label: "Pts", width: PTS_WIDTH, numeric: true, firstDir: SORT_DIR.DESC, field: "points",
      cell: (r) => fmtPts(r.engine.points) },
    // Sorts by rank, which already orders by Ordo with the unrated bracketed.
    { key: "ordo", label: "Ordo", width: ORDO_WIDTH, numeric: true, firstDir: SORT_DIR.DESC, field: "ordoKey",
      cell: (r) => ordoCell(r.engine) },
  ];
}

// Rank-number headers name their engine on hover. The ranking changes per
// render while the header persists, and a layout change rebuilds the header,
// so this runs after every body rebuild (onRender).
function titleRankHeaders(el) {
  const ths = el._table.headTable.tHead.rows[0].cells;
  el._names.forEach((name, j) => { ths[j + 1].title = name; });
}

// The factory fixes its columns at build time: (re)build for `n` engines.
function buildTable(el, n) {
  el._table?.wrap.remove();
  const columns = crosstableColumns(n);
  const table = createTable({
    cls: "xt",
    tableClass: "wb-table xt-tbl",
    columns,
    layoutKey: STORAGE_KEY.TOURNAMENTS_CROSSTABLE_LAYOUT,
    minPct: XT_MIN_PCT,
    traits: {
      sort: { sortKey: STORAGE_KEY.TOURNAMENTS_CROSSTABLE_SORT, single: true },
      resize: true, hide: true, fit: true, rowNav: {},
    },
    rowAttrs: (r) => ({ "data-id": r.engine.name }),
    onRender: () => titleRankHeaders(el),
  });
  table.wrap.style.setProperty(COLS_VAR, columns.length);
  markSelectable(table.headTable);
  markSelectable(table.bodyTable);
  el.append(table.wrap);
  el._table = table;
  el._n = n;
}

export function makeCrosstableBody() {
  const el = document.createElement("div");
  el.className = "wb-crosstable";
  el.innerHTML = `<div class="wb-empty">${LOADING_MSG}</div>`;
  el._table = null;
  el._n = 0;
  // Engine names in rank order (titleRankHeaders).
  el._names = [];
  return el;
}

// Fill a crosstable body (from makeCrosstableBody) from a tournament detail
// payload. Tolerates null detail (shows the empty state).
export function renderCrosstable(el, detail) {
  if (!el) return;
  const emptyEl = el.querySelector(".wb-empty");
  const engines = detail?.standings?.engines || [];
  const games = detail?.games || [];
  const empty = engines.length === 0 || games.length === 0;
  emptyEl.textContent = NO_GAMES_MSG;
  emptyEl.hidden = !empty;
  if (el._table) el._table.wrap.hidden = empty;
  if (empty) return;
  const ranked = rankEngines(engines);
  el._names = ranked.map((e) => e.name);
  if (ranked.length !== el._n) buildTable(el, ranked.length);
  const byPair = gamesByPair(games);
  el._table.render(ranked.map((engine, i) => ({
    rank: i + 1, engine, cells: ranked.map((col) => scoreCell(engine, col, byPair)),
    games: engine.games, points: engine.points, ordoKey: -(i + 1),
  })));
}
