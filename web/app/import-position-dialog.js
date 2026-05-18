// Import a position into a new HVE game from FEN or PGN text.
// Stateless: no debounced validate; Open submits to /game/import and the
// response is the parse result. Errors surface in the status label.
// Recents (previously imported texts) are served by the server; the
// localStorage cache is metadata-only and used to render the dropdown
// before the server responds.

import { apiErrorDetail, showDialog } from "./dialogs.js";

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

function stripPly(summary) {
  return (summary || "").replace(/\s*\(ply\s+\d+\)/gi, "").trim();
}

function loadRecentsCache() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENTS_CACHE_KEY) || "[]");
    return Array.isArray(v) ? v.filter((e) => e && e.hash) : [];
  } catch {
    return [];
  }
}

function saveRecentsCache(entries) {
  // Metadata only -- never stash the full text here.
  const lean = entries.map((e) => ({
    hash: e.hash,
    format: e.format,
    summary: stripPly(e.summary),
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

/** Show import dialog; resolves to /game/import response on success or
 *  null on cancel. The dialog itself POSTs /game/import (so it can
 *  surface errors inline) and returns the parsed response to the
 *  caller, which just needs to act on the success. */
export function showImportPositionDialog({ api }) {
  return showDialog({
    label: "Open position",
    width: "560px",
    body: (resolve, dialog) => {
      let format = "fen";
      let submitting = false;

      const wrap = document.createElement("div");
      wrap.className = "import-pos-form";

      const tabs = document.createElement("wa-tab-group");
      tabs.placement = "top";
      tabs.innerHTML = `
        <wa-tab slot="nav" panel="fen">FEN</wa-tab>
        <wa-tab slot="nav" panel="pgn">PGN</wa-tab>
        <wa-tab-panel name="fen"></wa-tab-panel>
        <wa-tab-panel name="pgn"></wa-tab-panel>
      `;
      wrap.appendChild(tabs);

      const textareas = {};
      for (const name of ["fen", "pgn"]) {
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
      recentSel.style.minWidth = "180px";
      let recentsCache = loadRecentsCache();
      function makeOption(entry, i) {
        const opt = document.createElement("wa-option");
        opt.value = String(i);
        opt.dataset.hash = entry.hash;
        const label = (stripPly(entry.summary) || entry.hash.slice(0, 12)).replace(/"/g, "&quot;");
        opt.innerHTML = `${entry.format.toUpperCase()} -- ${label}` +
          `<button slot="end" class="recent-del" title="Remove from history" aria-label="Remove">` +
          `<wa-icon name="trash"></wa-icon></button>`;
        const btn = opt.querySelector("button.recent-del");
        // wa-select listens for `mouseup` on the listbox container and
        // routes it through handleOptionClick -> hide(). Stop both phases
        // so the listbox stays open.
        const stop = (ev) => ev.stopPropagation();
        btn.addEventListener("mousedown", stop);
        btn.addEventListener("mouseup", stop);
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
          // Optimistic: remove from cache + DOM immediately so the
          // dropdown feels snappy. Roll back on server failure.
          const removed = entry;
          recentsCache = recentsCache.filter((x) => x.hash !== removed.hash);
          saveRecentsCache(recentsCache);
          opt.remove();
          if (!recentsCache.length) recentSel.style.visibility = "hidden";
          api("DELETE", `/game/recent-imports/${removed.hash}`).catch((e) => {
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
          setStatus(stripPly(r.summary) || "Loaded from history.", "ok");
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
          recentsCache = r.entries || [];
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
      status.textContent = EMPTY_PROMPT.fen;
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
        setStatus("Importing...", "muted");
        try {
          const r = await api("POST", "/game/import", { format, text });
          // Update the local recents cache from the server's response
          // so subsequent opens of the dialog see the new entry. The
          // freshest order comes from the next GET; this is just an
          // immediate-write so the user doesn't see their just-imported
          // entry missing.
          if (r.hash) {
            recentsCache = [
              { hash: r.hash, format, summary: stripPly(r.summary), ts: Date.now() },
              ...recentsCache.filter((e) => e.hash !== r.hash),
            ];
            saveRecentsCache(recentsCache);
          }
          resolve({ format, text, hash: r.hash, response: r });
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
