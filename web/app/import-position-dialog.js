// Import a position into a new HVE game from FEN or PGN text.
// Open validates via /game/import/validate (parse errors surface inline)
// and resolves with {format, text, hash, summary}; the caller owns the
// actual /game/import POST so it can do hash comparison first.
// Recents (previously imported texts) are served by the server; the
// localStorage cache is metadata-only and used to render the dropdown
// before the server responds. Cache is updated optimistically on validate.

import { apiErrorDetail, showDialog } from "./dialogs.js";

// Format a summary dict {white, black, result, side_to_move, fen} into a
// display string. `short: true` returns a compact form for tight UI (e.g.
// drop-down rows): for FEN imports, only the piece-placement field.
// Returns null when there is nothing meaningful to show.
export function formatSummary(s, { short = false } = {}) {
  if (!s) return null;
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
const ANALYSIS_WARNING = "Analysis in progress will be cancelled.";
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
  const rows = [
    ["Current",      currentSummary,  REPLACE_CURRENT_FALLBACK],
    ["Replace with", incomingSummary, REPLACE_INCOMING_FALLBACK],
  ].map(([label, s, fallback]) => ({
    label,
    display: formatSummary(s, { short: true }) || fallback,
    full:    formatSummary(s) || fallback,
  }));
  return showDialog({
    label: "",
    width: "min(480px, 92vw)",
    defaultValue: false,
    body: (resolve, dialog) => {
      dialog.setAttribute("no-header", "");

      const wrap = document.createElement("div");
      wrap.className = "replace-view-confirm";

      const title = document.createElement("div");
      title.className = "replace-view-title";
      title.textContent = "Replace game in viewer?";
      wrap.appendChild(title);

      for (const { label, display, full } of rows) {
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
      ok.textContent = "Replace";
      ok.addEventListener("click", () => resolve(true));

      dialog.append(cancel, ok);

      // Autofocus OK so Enter activates it natively; Tab to Cancel + Enter
      // then activates Cancel. Escape is handled by <wa-dialog>.
      requestAnimationFrame(() => ok.focus?.());
    },
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

const RECENTS_CACHE_KEY = "sturddle:import:recent";
const RECENTS_DISPLAY_CAP = 10;

function loadRecentsCache() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENTS_CACHE_KEY) || "[]");
    return Array.isArray(v) ? v.filter((e) => e && e.hash).slice(0, RECENTS_DISPLAY_CAP) : [];
  } catch {
    return [];
  }
}

function saveRecentsCache(entries) {
  // Metadata only -- never stash the full text here.
  const lean = entries.slice(0, RECENTS_DISPLAY_CAP).map((e) => ({
    hash: e.hash,
    format: e.format,
    summary: e.summary,
    ts: e.ts,
  }));
  try {
    localStorage.setItem(RECENTS_CACHE_KEY, JSON.stringify(lean));
  } catch {
    // localStorage may be disabled -- silently skip
  }
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
    label: "Open position",
    width: "560px",
    body: (resolve, dialog) => {
      let format = "pgn";
      let submitting = false;

      const wrap = document.createElement("div");
      wrap.className = "import-pos-form";

      const tabs = document.createElement("wa-tab-group");
      tabs.placement = "top";
      tabs.innerHTML = `
        <wa-tab slot="nav" panel="pgn">PGN</wa-tab>
        <wa-tab slot="nav" panel="fen">FEN</wa-tab>
        <wa-tab-panel name="pgn"></wa-tab-panel>
        <wa-tab-panel name="fen"></wa-tab-panel>
      `;
      wrap.appendChild(tabs);

      const textareas = {};
      for (const name of ["pgn", "fen"]) {
        const ta = document.createElement("wa-textarea");
        ta.size = "small";
        ta.resize = "vertical";
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
              setStatus(apiErrorDetail(e), "err");
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
          submit();
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
      start.addEventListener("click", submit);
      dialog.appendChild(start);

      function setStatus(msg, kind) {
        status.textContent = msg;
        status.classList.remove("muted", "ok", "err");
        status.classList.add(kind || "muted");
      }

      function selectTab(name) {
        if (typeof tabs.show === "function") tabs.show(name);
        format = name;
        if (!textareas[format].value.trim()) {
          setStatus(EMPTY_PROMPT[format], "muted");
        }
        syncSubmitEnabled();
      }

      function syncSubmitEnabled() {
        const has = (textareas[format].value || "").trim().length > 0;
        if (has && !submitting) start.removeAttribute("disabled");
        else start.setAttribute("disabled", "");
      }

      async function submit() {
        if (submitting) return;
        const text = textareas[format].value || "";
        if (!text.trim()) return;
        submitting = true;
        start.setAttribute("disabled", "");
        setStatus("Checking...", "muted");
        try {
          const r = await api("POST", "/game/import/validate", { format, text });
          if (r.hash) {
            recentsCache = [
              { hash: r.hash, format, summary: r.summary, ts: Date.now() },
              ...recentsCache.filter((e) => e.hash !== r.hash),
            ];
            saveRecentsCache(recentsCache);
          }
          resolve({ format, text, hash: r.hash, summary: r.summary });
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
        setStatus(`Loaded ${f.name}. Click Open to parse.`, "muted");
      }

      tabs.addEventListener("wa-tab-show", (ev) => {
        const name = ev.detail?.name;
        if (name !== "fen" && name !== "pgn") return;
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
