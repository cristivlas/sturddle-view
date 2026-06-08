// Import a position into a new HVE game from FEN or PGN text.
// Open validates via /game/import/validate (parse errors surface inline)
// and resolves with {format, text, hash, summary}; the caller owns the
// actual /game/import POST so it can do hash comparison first.
// Recents (previously imported texts) are served by the server; the
// localStorage cache is metadata-only and used to render the dropdown
// before the server responds. Cache is updated optimistically on validate.

import { apiErrorDetail, apiErrorObject, showDialog, toast } from "./dialogs.js";
import { APP_EVT } from "./app-events.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { attachColumnResize } from "./col-resize.js";
import { loadJson, saveJson } from "./storage.js";

// Format a summary dict {white, black, result, side_to_move, fen} into a
// display string. `short: true` returns a compact form for tight UI (e.g.
// drop-down rows): for FEN imports, only the piece-placement field.
// Returns null when there is nothing meaningful to show.
export function formatSummary(s, { short = false } = {}) {
  if (!s) return null;
  // Opening imports are identified by their ECO + name, not player names.
  if (s.opening) return s.opening;
  const names = s.white && s.black ? `${s.white} vs ${s.black}` : (s.white || s.black || null);
  const outcome = s.result || (s.side_to_move ? `${s.side_to_move[0].toUpperCase()}${s.side_to_move.slice(1)} to move` : null);
  // FEN-only imports (no headers, no result) display the FEN itself.
  // If a future PGN summary ever carries `fen` alongside names/result, the
  // names/result path still wins.
  if (s.fen && !names && !s.result) return short ? s.fen.split(" ", 1)[0] : s.fen;
  if (names && outcome) return `${names}: ${outcome}`;
  return names || outcome || null;
}

const REPLACE_CURRENT_FALLBACK = "the current game";
const REPLACE_INCOMING_FALLBACK = "a different game";
const ANALYSIS_WARNING = "Analysis will be closed.";
const CONFIRM_TRUNC_MAX = 46;
const RECENT_TRUNC_MAX = 40;
const ELLIPSIS = "...";

// Shorten a string by keeping the head + tail with an ellipsis in the
// middle. Preserves both ends, which matters for FENs (back rank info
// lives at both extremes). Returns the input unchanged when already short.
function truncateMiddle(text, max) {
  if (!text || text.length <= max) return text;
  if (max <= ELLIPSIS.length) return text.slice(0, Math.max(0, max));
  const slot = max - ELLIPSIS.length;
  const head = Math.ceil(slot / 2);
  const tail = slot - head;
  return text.slice(0, head) + ELLIPSIS + text.slice(text.length - tail);
}

// Shared modal shell for "viewed game" confirms: title, labeled summary rows,
// optional analysis-cancel warning, Cancel/OK footer. Rows are pre-formatted
// {label, summary, fallback} entries.
function _showViewedGameConfirm({ title, rows, analysisRunning, okLabel }) {
  const formatted = rows.map(({ label, summary, fallback }) => ({
    label,
    display: formatSummary(summary, { short: true }) || fallback,
    full:    formatSummary(summary) || fallback,
  }));
  return showDialog({
    label: "",
    width: "min(480px, 92vw)",
    defaultValue: false,
    body: (resolve, dialog) => {
      dialog.setAttribute("no-header", "");

      const wrap = document.createElement("div");
      wrap.className = "replace-view-confirm";

      const titleEl = document.createElement("div");
      titleEl.className = "replace-view-title";
      titleEl.textContent = title;
      wrap.appendChild(titleEl);

      for (const { label, display, full } of formatted) {
        const row = document.createElement("div");
        row.className = "replace-view-row";
        const k = document.createElement("div");
        k.className = "replace-view-label";
        k.textContent = `${label}:`;
        const v = document.createElement("div");
        v.className = "replace-view-summary";
        v.textContent = truncateMiddle(display, CONFIRM_TRUNC_MAX);
        v.title = full;
        row.append(k, v);
        wrap.appendChild(row);
      }

      if (analysisRunning) {
        const warn = document.createElement("div");
        warn.className = "replace-view-warning";
        warn.textContent = ANALYSIS_WARNING;
        wrap.appendChild(warn);
      }

      dialog.appendChild(wrap);

      const cancel = document.createElement("wa-button");
      cancel.slot = "footer";
      cancel.size = "small";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => resolve(false));

      const ok = document.createElement("wa-button");
      ok.slot = "footer";
      ok.size = "small";
      ok.variant = "brand";
      ok.textContent = okLabel;
      ok.addEventListener("click", () => resolve(true));

      dialog.append(cancel, ok);

      // Autofocus OK so Enter activates it natively; Tab to Cancel + Enter
      // then activates Cancel. Escape is handled by <wa-dialog>.
      requestAnimationFrame(() => ok.focus?.());
    },
  });
}

// Confirm before replacing the game currently shown in the viewer.
// Skips the prompt (returns true) when the incoming hash matches the current
// view, or when nothing is being viewed. Resolves false on cancel.
export function confirmReplaceViewedGame({
  currentHash,
  currentSummary,
  incomingHash,
  incomingSummary,
  analysisRunning = false,
}) {
  if (incomingHash && currentHash && incomingHash === currentHash) return Promise.resolve(true);
  return _showViewedGameConfirm({
    title: "Replace game in viewer?",
    rows: [
      { label: "Current",      summary: currentSummary,  fallback: REPLACE_CURRENT_FALLBACK },
      { label: "Replace with", summary: incomingSummary, fallback: REPLACE_INCOMING_FALLBACK },
    ],
    analysisRunning,
    okLabel: "Replace",
  });
}

// Confirm before leaving the currently viewed game (e.g. starting a fresh
// game from the ribbon). Single "Current" row; same analysis warning when
// analysis is in flight. Skips the prompt (returns true) when not viewing.
// Resolves false on cancel.
export function confirmDiscardViewedGame({ viewing, currentSummary, analysisRunning = false }) {
  if (!viewing) return Promise.resolve(true);
  return _showViewedGameConfirm({
    title: "Leave the viewed game?",
    rows: [
      { label: "Current", summary: currentSummary, fallback: REPLACE_CURRENT_FALLBACK },
    ],
    analysisRunning,
    okLabel: "New game",
  });
}

const PLACEHOLDERS = {
  fen: "Paste FEN, e.g.\nrnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  pgn: 'Paste PGN, e.g.\n[Event "?"]\n[White "..."]\n[Black "..."]\n\n1. e4 e5 2. Nf3 Nc6 ...',
};
const EMPTY_PROMPT = {
  fen: "Paste a FEN to begin.",
  pgn: "Paste a PGN to begin.",
};
const TEXTAREA_ROWS = 8;

const OPENINGS_DEFAULT_PCTS = [12, 50, 38];
const OPENINGS_COL_MIN_PCT = 8;

// Combining diacritical marks block (U+0300-U+036F). Built via RegExp ctor
// from hex escapes so the source stays ASCII-only.
const COMBINING_MARKS_RE = new RegExp("[\\u0300-\\u036f]", "g");

// Lowercase and strip diacritics so "Goring"/"Renee" match "Goring"/"Renee"
// with their umlaut/accent. NFD splits each accented char into base + mark;
// we drop the marks.
function foldDiacritics(s) {
  return (s || "").normalize("NFD").replace(COMBINING_MARKS_RE, "").toLowerCase();
}

// The opening list is static for the app's lifetime, so fetch it once per
// client and share it across every dialog open. Caches the in-flight promise
// (not just the result) so concurrent opens don't fire parallel requests.
let _openingsPromise = null;
function loadOpeningsOnce(api) {
  if (!_openingsPromise) {
    _openingsPromise = api("GET", "/openings")
      .then((r) => {
        const rows = r.results || [];
        // Precompute a diacritic-folded name per row so the per-keystroke
        // filter doesn't re-normalize the whole list.
        for (const row of rows) row._fold = foldDiacritics(row.name);
        return rows;
      })
      .catch((e) => { _openingsPromise = null; throw e; });  // allow retry
  }
  return _openingsPromise;
}

// Build the Openings tab: a resizable-column table (ECO / Name / Moves)
// loaded once, filtered locally, with a toggle-overlay search bar that
// mirrors the Engines list. Selecting a row exposes its PGN via
// selectedPgn(); the host feeds it into the same import flow as the PGN tab.
function createOpeningsPanel({ api, onChange, onCommit }) {
  let selectedPgn = "";
  let selectedRow = null;
  let rows = [];
  let filterText = "";
  let sortOrder = "none";  // none = backend order (ECO, name); else by name

  const el = document.createElement("div");
  el.className = "openings-panel";
  el.innerHTML = `
    <div class="openings-table-wrap">
      <table class="openings-table">
        <colgroup>
          <col class="openings-col-eco">
          <col class="openings-col-name">
          <col class="openings-col-moves">
        </colgroup>
        <thead>
          <tr>
            <th>ECO<span class="th-grip"></span></th>
            <th>Name<span class="th-grip"></span></th>
            <th>Moves</th>
          </tr>
        </thead>
        <tbody class="openings-list" role="listbox" tabindex="0"></tbody>
      </table>
    </div>
    <div class="openings-ribbon" role="toolbar" aria-label="Opening actions">
      <button class="ribbon-btn openings-search-btn" type="button" aria-label="Search openings" title="Search openings">
        <wa-icon name="magnifying-glass"></wa-icon>
      </button>
      <button class="ribbon-btn openings-sort-asc" type="button" aria-label="Sort A-Z" title="Sort A-Z">
        <wa-icon name="arrow-down-a-z"></wa-icon>
      </button>
      <button class="ribbon-btn openings-sort-desc" type="button" aria-label="Sort Z-A" title="Sort Z-A">
        <wa-icon name="arrow-down-z-a"></wa-icon>
      </button>
    </div>
    <div class="openings-search-wrap">
      <wa-input class="openings-search" size="small" placeholder="Search by name or ECO (e.g. Sicilian, B12)..." clearable autocomplete="off"></wa-input>
    </div>
  `;

  const list = el.querySelector(".openings-list");

  function filtered() {
    const needle = foldDiacritics(filterText.trim());
    let out = needle
      ? rows.filter((r) => r._fold.includes(needle) || r.eco.toLowerCase().includes(needle))
      : rows.slice();
    if (sortOrder === "asc" || sortOrder === "desc") {
      const dir = sortOrder === "asc" ? 1 : -1;
      out.sort((a, b) => dir * a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
    }
    return out;
  }

  function clearSelection() {
    selectedPgn = "";
    selectedRow = null;
    for (const r of list.querySelectorAll("tr.selected")) r.classList.remove("selected");
    onChange?.();
  }

  function scrollSelectedIntoView() {
    list.querySelector("tr.selected")?.scrollIntoView({ block: "nearest" });
  }

  function renderList() {
    list.replaceChildren();
    const visible = filtered();
    if (!visible.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 3;
      td.className = "openings-list-empty muted";
      td.textContent = rows.length ? "No openings match." : "Loading...";
      tr.appendChild(td);
      list.appendChild(tr);
      return;
    }
    for (const row of visible) {
      const tr = document.createElement("tr");
      tr.className = "openings-list-item";

      const ecoTd = document.createElement("td");
      ecoTd.className = "openings-list-eco";
      ecoTd.textContent = row.eco;

      const nameTd = document.createElement("td");
      nameTd.className = "openings-list-name";
      nameTd.textContent = row.name;
      nameTd.title = row.name;

      const movesTd = document.createElement("td");
      movesTd.className = "openings-list-moves";
      movesTd.textContent = row.pgn;
      movesTd.title = row.pgn;

      tr.append(ecoTd, nameTd, movesTd);
      // Re-apply the highlight to the still-selected row after a re-render
      // so the pick stays visible (e.g. when the search filter is cleared).
      if (selectedRow && row === selectedRow) tr.classList.add("selected");
      const pick = () => {
        for (const r of list.querySelectorAll("tr.selected")) r.classList.remove("selected");
        tr.classList.add("selected");
        selectedPgn = row.pgn;
        selectedRow = row;
        onChange?.();
      };
      tr.addEventListener("click", pick);
      tr.addEventListener("dblclick", () => { pick(); onCommit?.(); });
      list.appendChild(tr);
    }
  }

  list.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && selectedPgn) { ev.preventDefault(); onCommit?.(); }
  });

  // Toggle-overlay search, mirroring the Engines list.
  {
    const searchBtn = el.querySelector(".openings-search-btn");
    const searchWrap = el.querySelector(".openings-search-wrap");
    const searchInput = el.querySelector(".openings-search");
    const tableWrap = el.querySelector(".openings-table-wrap");

    searchInput.addEventListener("input", () => {
      filterText = searchInput.value || "";
      searchBtn.classList.toggle("is-active", !!filterText);
      clearSelection();
      renderList();
    });

    function closeSearch() {
      searchWrap.classList.remove("open");
      searchBtn.classList.remove("is-active");
      searchInput.value = "";
      filterText = "";
      renderList();
      // Clearing the filter re-renders the full list; keep the picked row
      // in view so the selection doesn't scroll off-screen.
      scrollSelectedIntoView();
      document.removeEventListener("pointerdown", onOutsideClick);
      document.removeEventListener("keydown", onSearchKey, true);
    }
    function onOutsideClick(e) {
      if (searchWrap.contains(e.target) || searchBtn.contains(e.target) || tableWrap.contains(e.target)) return;
      closeSearch();
    }
    // Capture-phase so Escape closes the search bar, not the whole dialog.
    function onSearchKey(e) {
      if (e.key === "Escape") { closeSearch(); e.preventDefault(); e.stopPropagation(); }
    }
    searchBtn.addEventListener("click", () => {
      const opening = !searchWrap.classList.contains("open");
      if (opening) {
        searchWrap.classList.add("open");
        searchBtn.classList.add("is-active");
        requestAnimationFrame(() => searchInput.focus?.());
        document.addEventListener("pointerdown", onOutsideClick);
        document.addEventListener("keydown", onSearchKey, true);
      } else {
        closeSearch();
      }
    });
    // Committing a row (dbl-click / Enter) closes the whole dialog without
    // routing through closeSearch, so tear the document-level listeners down
    // on dialog hide too -- otherwise they leak across open/close cycles.
    requestAnimationFrame(() => {
      const dialog = el.closest("wa-dialog");
      dialog?.addEventListener("wa-after-hide", (ev) => {
        if (ev.target !== dialog) return;
        document.removeEventListener("pointerdown", onOutsideClick);
        document.removeEventListener("keydown", onSearchKey, true);
      });
    });
  }

  // Sort A-Z / Z-A, mirroring the Engines list (third click clears).
  {
    const ascBtn = el.querySelector(".openings-sort-asc");
    const descBtn = el.querySelector(".openings-sort-desc");
    function syncSortButtons() {
      ascBtn.classList.toggle("is-active", sortOrder === "asc");
      descBtn.classList.toggle("is-active", sortOrder === "desc");
    }
    function setSort(next) {
      sortOrder = sortOrder === next ? "none" : next;
      syncSortButtons();
      // Reordering invalidates the visible pick (same as filtering); drop it
      // so Open never imports a selection the user can no longer see.
      clearSelection();
      renderList();
    }
    ascBtn.addEventListener("click", () => setSort("asc"));
    descBtn.addEventListener("click", () => setSort("desc"));
  }

  // Column resize over the three cols.
  {
    const colEls = Array.from(el.querySelectorAll(".openings-table col"));
    const tableEl = el.querySelector(".openings-table");
    const wrapEl = el.querySelector(".openings-table-wrap");
    const grips = Array.from(el.querySelectorAll(".openings-table .th-grip"));
    const colPcts = OPENINGS_DEFAULT_PCTS.slice();
    attachColumnResize({
      table: tableEl,
      grips,
      overlayHost: wrapEl,
      storageKey: STORAGE_KEY.OPENINGS_COL_PCTS,
      sizes: colPcts,
      unit: "pct",
      applySizes(sizes, ctx) {
        if (ctx) {
          const { deltaFrac, startSizes, gripIdx } = ctx;
          const dPct = deltaFrac * 100;
          let a = startSizes[gripIdx] + dPct;
          let b = startSizes[gripIdx + 1] - dPct;
          if (a < OPENINGS_COL_MIN_PCT) { b -= OPENINGS_COL_MIN_PCT - a; a = OPENINGS_COL_MIN_PCT; }
          if (b < OPENINGS_COL_MIN_PCT) { a -= OPENINGS_COL_MIN_PCT - b; b = OPENINGS_COL_MIN_PCT; }
          sizes[gripIdx] = a;
          sizes[gripIdx + 1] = b;
        }
        colEls.forEach((c, i) => { c.style.width = sizes[i] + "%"; });
      },
    });
  }

  async function load() {
    try {
      rows = await loadOpeningsOnce(api);
    } catch {
      rows = [];
    }
    renderList();
  }

  // Anchor the slide-up search bar just above the ribbon (matches Engines).
  // Must run when the panel is visible -- measuring at build time (panel
  // still in an inactive tab) yields 0 and the bar misaligns.
  function measureRibbon() {
    const h = el.querySelector(".openings-ribbon")?.offsetHeight || 0;
    el.style.setProperty("--openings-ribbon-h", h + "px");
  }

  renderList();

  return {
    el,
    load,
    measureRibbon,
    selectedPgn: () => selectedPgn,
    selectedOpening: () => (selectedRow ? { eco: selectedRow.eco, name: selectedRow.name } : null),
    focus: () => requestAnimationFrame(() => el.querySelector(".openings-search-btn")?.focus?.()),
  };
}

const RECENTS_CACHE_KEY = STORAGE_KEY.IMPORT_RECENTS;
const RECENTS_DISPLAY_CAP = 10;

function loadRecentsCache() {
  const v = loadJson(RECENTS_CACHE_KEY, []);
  return Array.isArray(v) ? v.filter((e) => e && e.hash).slice(0, RECENTS_DISPLAY_CAP) : [];
}

function saveRecentsCache(entries) {
  // Metadata only -- never stash the full text here.
  const lean = entries.slice(0, RECENTS_DISPLAY_CAP).map((e) => ({
    hash: e.hash,
    format: e.format,
    summary: e.summary,
    ts: e.ts,
  }));
  saveJson(RECENTS_CACHE_KEY, lean);
}

function detectFormatFromName(name) {
  const n = (name || "").toLowerCase();
  if (n.endsWith(".pgn")) return "pgn";
  if (n.endsWith(".fen") || n.endsWith(".epd")) return "fen";
  return null;
}

/** Show import dialog; resolves to {format, text, hash, summary} on Open,
 *  or null on cancel. The dialog validates via /game/import/validate (parse
 *  errors surface inline) but does NOT import -- the caller owns the import
 *  POST so it can do hash comparison and confirmation first. */
export function showImportPositionDialog({ api }) {
  return showDialog({
    label: "Import",
    width: "560px",
    body: (resolve, dialog) => {
      let format = "pgn";
      let submitting = false;

      const wrap = document.createElement("div");
      wrap.className = "import-pos-form";

      const tabs = document.createElement("wa-tab-group");
      tabs.className = "import-pos-tabs";
      tabs.placement = "top";
      tabs.innerHTML = `
        <wa-tab slot="nav" panel="pgn">PGN</wa-tab>
        <wa-tab slot="nav" panel="fen">FEN</wa-tab>
        <wa-tab slot="nav" panel="openings">Opening</wa-tab>
        <wa-tab-panel name="pgn"></wa-tab-panel>
        <wa-tab-panel name="fen"></wa-tab-panel>
        <wa-tab-panel name="openings"></wa-tab-panel>
      `;
      wrap.appendChild(tabs);

      const openings = createOpeningsPanel({
        api,
        onChange: () => { if (format === "openings") syncSubmitEnabled(); },
        onCommit: () => { if (format === "openings") submit(); },
      });
      tabs.querySelector('wa-tab-panel[name="openings"]').appendChild(openings.el);

      const textareas = {};
      for (const name of ["pgn", "fen"]) {
        const ta = document.createElement("wa-textarea");
        ta.size = "small";
        ta.resize = "none";
        ta.rows = TEXTAREA_ROWS;
        ta.placeholder = PLACEHOLDERS[name];
        ta.style.fontFamily = "var(--mono-font, monospace)";
        ta.style.width = "100%";
        ta.addEventListener("input", () => {
          if (format !== name) return;
          syncSubmitEnabled();
        });
        tabs.querySelector(`wa-tab-panel[name="${name}"]`).appendChild(ta);
        textareas[name] = ta;
      }

      // Toolbar row: From file + Recent dropdown.
      const toolbar = document.createElement("div");
      toolbar.className = "import-pos-toolbar";

      const fileBtn = document.createElement("wa-button");
      fileBtn.size = "small";
      fileBtn.innerHTML = `<wa-icon slot="start" name="upload"></wa-icon>From file...`;
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = ".fen,.pgn,.epd,text/plain";
      fileInput.style.display = "none";
      fileBtn.addEventListener("click", () => fileInput.click());
      fileInput.addEventListener("change", async () => {
        const f = fileInput.files?.[0];
        if (!f) return;
        await ingestFile(f);
        fileInput.value = "";
      });
      toolbar.append(fileBtn, fileInput);

      const recentSel = document.createElement("wa-select");
      recentSel.size = "small";
      recentSel.placeholder = "Recent...";
      let recentsCache = loadRecentsCache();
      function makeOption(entry, i) {
        const opt = document.createElement("wa-option");
        opt.value = String(i);
        opt.dataset.hash = entry.hash;
        const full = formatSummary(entry.summary, { short: true }) || entry.hash.slice(0, 12);
        const shown = truncateMiddle(full, RECENT_TRUNC_MAX);
        if (shown !== full) opt.title = full;
        // textContent on the label (no innerHTML) blocks any HTML-injection
        // from PGN headers in the summary.
        const labelEl = document.createElement("span");
        labelEl.className = "recent-label";
        labelEl.textContent = shown;
        opt.append(labelEl);
        const delBtn = document.createElement("button");
        delBtn.slot = "end";
        delBtn.className = "recent-del";
        delBtn.title = "Remove from history";
        delBtn.setAttribute("aria-label", "Remove");
        const delIcon = document.createElement("wa-icon");
        delIcon.setAttribute("name", "trash");
        delBtn.append(delIcon);
        opt.append(delBtn);
        // wa-select listens for `mouseup` on the listbox container and
        // routes it through handleOptionClick -> hide(). Stop both phases
        // so the listbox stays open.
        const stop = (ev) => ev.stopPropagation();
        delBtn.addEventListener("mousedown", stop);
        delBtn.addEventListener("mouseup", stop);
        delBtn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
          // Optimistic: remove from cache + DOM immediately so the
          // dropdown feels snappy. Roll back on server failure.
          const removed = entry;
          recentsCache = recentsCache.filter((x) => x.hash !== removed.hash);
          saveRecentsCache(recentsCache);
          opt.remove();
          if (!recentsCache.length) recentSel.style.visibility = "hidden";
          api("DELETE", `/game/recent-imports/${removed.hash}`)
            .then(async () => {
              // Notify other perspectives that recents changed so they
              // can refresh derived state (e.g. play.js x-game info,
              // for the fork glyph + banner). Bus-style decoupling so
              // the dialog stays unaware of who is listening.
              window.dispatchEvent(new CustomEvent(APP_EVT.RECENTS_CHANGED, {
                detail: { deletedHash: removed.hash },
              }));
              if (recentSel.querySelectorAll("wa-option").length >= RECENTS_DISPLAY_CAP) return;
              try {
                const r = await api("GET", "/game/recent-imports");
                const known = new Set(recentsCache.map((c) => c.hash));
                const fresh = (r.entries || []).filter(
                  (e) => e.hash !== removed.hash && !known.has(e.hash)
                );
                if (!fresh.length) return;
                recentsCache = [...recentsCache, ...fresh].slice(0, RECENTS_DISPLAY_CAP);
                saveRecentsCache(recentsCache);
                renderRecents();
              } catch (err) { /* offline -- keep current cache */ }
            })
            .catch((e) => {
              recentsCache = [removed, ...recentsCache];
              saveRecentsCache(recentsCache);
              renderRecents();
              // Friendly toast for the "blocked by live forks" 409 case
              // (xgame nav). Falls back to the generic error otherwise.
              const obj = apiErrorObject(e);
              if (obj?.error === "has_children") {
                const n = Array.isArray(obj.children) ? obj.children.length : 0;
                const msg = n === 1
                  ? "Cannot delete: this game has 1 forked variation."
                  : `Cannot delete: this game has ${n} forked variations.`;
                toast(msg, { variant: "warning" });
              } else {
                setStatus(apiErrorDetail(e), "err");
              }
            });
        });
        return opt;
      }
      function renderRecents() {
        const shown = recentsCache.slice(0, RECENTS_DISPLAY_CAP);
        recentSel.replaceChildren(...shown.map((e, i) => makeOption(e, i)));
        recentSel.style.visibility = shown.length ? "" : "hidden";
      }
      recentSel.addEventListener("change", async () => {
        const entry = recentsCache[Number(recentSel.value)];
        recentSel.value = "";
        if (!entry) return;
        try {
          const r = await api("GET", `/game/recent-imports/${entry.hash}`);
          const targetFormat = r.format || entry.format;
          if (targetFormat !== format) selectTab(targetFormat);
          textareas[targetFormat].value = r.text || "";
          syncSubmitEnabled();
          // An opening saved to recents reloads as a PGN; recover its
          // {eco, name} from the stored label so the re-import keeps the
          // opening identity (sides + label) instead of becoming a plain
          // game with "?" players.
          const openingOverride = openingFromLabel(
            (r.summary && r.summary.opening) || entry.summary?.opening,
          );
          submit(openingOverride);
        } catch (e) {
          setStatus(apiErrorDetail(e), "err");
        }
      });
      renderRecents();
      // Fire-and-forget refresh from the server. Renders happen
      // immediately from the local cache, then again when the server
      // responds so the user sees the freshest list without delay.
      (async () => {
        try {
          const r = await api("GET", "/game/recent-imports");
          recentsCache = (r.entries || []).slice(0, RECENTS_DISPLAY_CAP);
          saveRecentsCache(recentsCache);
          renderRecents();
        } catch {
          // Offline / unauthenticated: keep the local cache as-is.
        }
      })();
      toolbar.appendChild(recentSel);

      wrap.appendChild(toolbar);

      const status = document.createElement("div");
      status.className = "import-pos-status muted";
      status.textContent = EMPTY_PROMPT.pgn;
      wrap.appendChild(status);

      dialog.appendChild(wrap);

      const start = document.createElement("wa-button");
      start.slot = "footer";
      start.size = "small";
      start.variant = "brand";
      start.textContent = "Open";
      start.setAttribute("disabled", "");
      start.addEventListener("click", () => submit());
      dialog.appendChild(start);

      function setStatus(msg, kind) {
        status.textContent = msg;
        status.classList.remove("muted", "ok", "err");
        status.classList.add(kind || "muted");
      }

      function selectTab(name) {
        if (typeof tabs.show === "function") tabs.show(name);
        format = name;
        // Toolbar (file/recents) and the status line are meaningless on the
        // Openings tab; remove them there. The Opening panel grows to fill
        // the reclaimed space (CSS) so the dialog height stays constant.
        toolbar.style.display = name === "openings" ? "none" : "";
        status.style.display = name === "openings" ? "none" : "";
        if (name === "openings") {
          openings.load();
          requestAnimationFrame(openings.measureRibbon);
          openings.focus();
        } else if (!textareas[format].value.trim()) {
          setStatus(EMPTY_PROMPT[format], "muted");
        }
        syncSubmitEnabled();
      }

      // The text the import POST will carry, regardless of source tab.
      // Openings resolve to a PGN; that's the format we send.
      function currentText() {
        return format === "openings" ? openings.selectedPgn() : (textareas[format].value || "");
      }
      function sendFormat() {
        return format === "openings" ? "pgn" : format;
      }

      function syncSubmitEnabled() {
        const has = currentText().trim().length > 0;
        if (has && !submitting) start.removeAttribute("disabled");
        else start.setAttribute("disabled", "");
      }

      // Split a stored "B12 Some Opening: Variation" label back into
      // {eco, name}. ECO is the leading [A-E]NN token; the rest is the name.
      // Returns null when the label has no recognizable ECO prefix.
      function openingFromLabel(label) {
        if (typeof label !== "string") return null;
        const m = label.match(/^([A-E]\d{2})\s+(.+)$/);
        return m ? { eco: m[1], name: m[2] } : null;
      }

      async function submit(openingOverride = null) {
        if (submitting) return;
        const text = currentText();
        if (!text.trim()) return;
        submitting = true;
        start.setAttribute("disabled", "");
        setStatus("Checking...", "muted");
        const fmt = sendFormat();
        const opening = openingOverride
          || (format === "openings" ? openings.selectedOpening() : null);
        try {
          const r = await api("POST", "/game/import/validate", { format: fmt, text });
          // Openings are labeled by ECO + name everywhere (recents, confirm
          // dialog, viewer); fold it into the summary the caller carries.
          const summary = opening
            ? { ...(r.summary || {}), opening: `${opening.eco} ${opening.name}`.trim() }
            : r.summary;
          if (r.hash) {
            recentsCache = [
              { hash: r.hash, format: fmt, summary, ts: Date.now() },
              ...recentsCache.filter((e) => e.hash !== r.hash),
            ];
            saveRecentsCache(recentsCache);
          }
          resolve({ format: fmt, text, hash: r.hash, summary, opening });
        } catch (e) {
          submitting = false;
          setStatus(apiErrorDetail(e), "err");
          syncSubmitEnabled();
        }
      }

      // Reading a file just stuffs its text into the matching tab. No
      // pre-validation -- the user clicks Open to find out if it parses.
      async function ingestFile(f) {
        const text = await f.text();
        const hint = detectFormatFromName(f.name) ?? format;
        if (hint !== format) selectTab(hint);
        textareas[hint].value = text;
        syncSubmitEnabled();
        setStatus(`Loaded ${f.name}`, "muted");
      }

      tabs.addEventListener("wa-tab-show", (ev) => {
        const name = ev.detail?.name;
        if (name !== "fen" && name !== "pgn" && name !== "openings") return;
        selectTab(name);
      });

      // Drag-and-drop a .fen / .pgn file anywhere on the dialog body.
      wrap.addEventListener("dragover", (ev) => {
        if (ev.dataTransfer?.types?.includes("Files")) {
          ev.preventDefault();
          wrap.classList.add("drag-over");
        }
      });
      wrap.addEventListener("dragleave", () => wrap.classList.remove("drag-over"));
      wrap.addEventListener("drop", async (ev) => {
        wrap.classList.remove("drag-over");
        const f = ev.dataTransfer?.files?.[0];
        if (!f) return;
        ev.preventDefault();
        await ingestFile(f);
      });
    },
  });
}
