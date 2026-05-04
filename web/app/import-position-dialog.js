// Import a position into a new HVE game from FEN or PGN text.
// Validates against the server on input change; the Start button stays
// disabled until a parse succeeds (no `*` markers, no native `required`,
// per the modern-dialog convention used elsewhere).

import { apiErrorDetail, showDialog } from "./dialogs.js";

const PLACEHOLDERS = {
  fen: "Paste FEN, e.g.\nrnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  pgn: 'Paste PGN, e.g.\n[Event "?"]\n[White "..."]\n[Black "..."]\n\n1. e4 e5 2. Nf3 Nc6 ...',
};
const EMPTY_PROMPT = {
  fen: "Paste a FEN to begin.",
  pgn: "Paste a PGN to begin.",
};
const TEXTAREA_ROWS = 8; // same on both tabs so the dialog doesn't resize

const RECENTS_KEY = "sturddle.import.recent";
const RECENTS_MAX = 5;

function loadRecents() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function saveRecent(entry) {
  // entry: { format, text, summary, ts }
  const cur = loadRecents().filter(
    (e) => !(e.format === entry.format && e.text === entry.text),
  );
  cur.unshift(entry);
  try {
    localStorage.setItem(RECENTS_KEY, JSON.stringify(cur.slice(0, RECENTS_MAX)));
  } catch {
    // localStorage may be disabled — silently skip
  }
}

function detectFormatFromName(name) {
  const n = (name || "").toLowerCase();
  if (n.endsWith(".pgn")) return "pgn";
  if (n.endsWith(".fen") || n.endsWith(".epd")) return "fen";
  return null;
}

/** Show import dialog; resolves to /game/import payload or null on cancel.
 *  Caller is responsible for POSTing the payload. */
export function showImportPositionDialog({ api }) {
  return showDialog({
    label: "Open position",
    width: "560px",
    body: (resolve, dialog) => {
      let format = "fen";
      let lastValid = null;
      let validateSeq = 0;
      let validateTimer = null;
      function cancelPendingValidate() {
        clearTimeout(validateTimer);
        validateTimer = null;
        validateSeq++;
      }

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
          clearTimeout(validateTimer);
          validateTimer = setTimeout(validate, 200);
        });
        tabs.querySelector(`wa-tab-panel[name="${name}"]`).appendChild(ta);
        textareas[name] = ta;
      }

      // Toolbar row: From file… + Recent dropdown.
      const toolbar = document.createElement("div");
      toolbar.className = "import-pos-toolbar";

      const fileBtn = document.createElement("wa-button");
      fileBtn.size = "small";
      fileBtn.innerHTML = `<wa-icon slot="start" name="upload"></wa-icon>From file…`;
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
      recentSel.placeholder = "Recent…";
      recentSel.style.minWidth = "180px";
      let recentsCache = loadRecents();
      function refreshRecents() {
        recentsCache = loadRecents();
        recentSel.innerHTML = recentsCache
          .map((e, i) => {
            const label = (e.summary || e.text.slice(0, 40)).replace(/"/g, "&quot;");
            return `<wa-option value="${i}">${e.format.toUpperCase()} — ${label}</wa-option>`;
          })
          .join("");
        recentSel.style.visibility = recentsCache.length ? "" : "hidden";
      }
      recentSel.addEventListener("change", () => {
        const entry = recentsCache[Number(recentSel.value)];
        if (!entry) return;
        if (entry.format !== format) selectTab(entry.format);
        textareas[entry.format].value = entry.text;
        recentSel.value = "";
        validate();
      });
      refreshRecents();
      toolbar.appendChild(recentSel);

      wrap.appendChild(toolbar);

      const status = document.createElement("div");
      status.className = "import-pos-status muted";
      status.textContent = EMPTY_PROMPT.fen;
      wrap.appendChild(status);

      const playAsRow = document.createElement("div");
      playAsRow.className = "import-pos-playas";
      playAsRow.innerHTML = `
        <label class="import-pos-playas-label">Play as</label>
        <wa-radio-group name="play-as" value="side_to_move" size="small" orientation="horizontal">
          <wa-radio value="white">White</wa-radio>
          <wa-radio value="black">Black</wa-radio>
          <wa-radio value="side_to_move">Side to move</wa-radio>
        </wa-radio-group>
      `;
      wrap.appendChild(playAsRow);

      dialog.appendChild(wrap);

      const start = document.createElement("wa-button");
      start.slot = "footer";
      start.size = "small";
      start.variant = "brand";
      start.textContent = "Start";
      start.setAttribute("disabled", "");
      start.addEventListener("click", () => {
        if (!lastValid) return;
        cancelPendingValidate();
        const playAs = playAsRow.querySelector("wa-radio-group").value;
        const text = textareas[format].value || "";
        saveRecent({
          format,
          text,
          summary: lastValid.summary,
          ts: Date.now(),
        });
        resolve({ format, text, human_side: playAs });
      });
      dialog.appendChild(start);

      function setStatus(msg, kind) {
        status.textContent = msg;
        status.classList.remove("muted", "ok", "err");
        status.classList.add(kind || "muted");
      }

      function selectTab(name) {
        if (typeof tabs.show === "function") tabs.show(name);
        format = name;
      }

      // Read a File, ask the server to auto-detect its format, switch to
      // that tab and load the text. Falls back to the filename hint and
      // finally to the currently-active tab if everything fails to parse.
      async function ingestFile(f) {
        const text = await f.text();
        const hint = detectFormatFromName(f.name);
        let target = hint ?? format;
        try {
          const r = await api("POST", "/game/import/validate", {
            format: hint ?? "auto",
            text,
          });
          if (r.detected_format === "fen" || r.detected_format === "pgn") {
            target = r.detected_format;
          }
        } catch {
          // server rejected — drop into the hinted/active tab and let the
          // normal validate() show the error.
        }
        if (target !== format) selectTab(target);
        textareas[target].value = text;
        validate();
      }

      async function validate() {
        const seq = ++validateSeq;
        const text = textareas[format].value || "";
        if (!text.trim()) {
          lastValid = null;
          start.setAttribute("disabled", "");
          setStatus(EMPTY_PROMPT[format], "muted");
          return;
        }
        try {
          const r = await api("POST", "/game/import/validate", { format, text });
          if (seq !== validateSeq) return;
          lastValid = r;
          start.removeAttribute("disabled");
          setStatus(r.summary, "ok");
        } catch (e) {
          if (seq !== validateSeq) return;
          lastValid = null;
          start.setAttribute("disabled", "");
          setStatus(apiErrorDetail(e), "err");
        }
      }

      tabs.addEventListener("wa-tab-show", (ev) => {
        const name = ev.detail?.name;
        if (name !== "fen" && name !== "pgn") return;
        cancelPendingValidate();
        format = name;
        validate();
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
