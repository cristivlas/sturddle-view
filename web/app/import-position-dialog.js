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

/**
 * @param {object} args
 * @param {Function} args.api - api(method, path, body?) -> Promise.
 * @returns {Promise<object|null>} the import payload to POST to /game/import,
 *   or null on cancel. Caller is responsible for calling /game/import.
 */
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
        validateSeq++; // also drop any in-flight response
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

      // One textarea per panel — re-parenting a single textarea on tab
      // switch breaks rendering inside wa-tab-panel.
      const textareas = {};
      for (const name of ["fen", "pgn"]) {
        const ta = document.createElement("wa-textarea");
        ta.size = "small";
        ta.resize = "vertical";
        ta.rows = name === "fen" ? 3 : 8;
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

      // Footer: Start only — wa-dialog provides its own X close button,
      // and showDialog treats a close-without-resolve as cancel.
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
        resolve({
          format,
          text: textareas[format].value || "",
          human_side: playAs,
        });
      });
      dialog.appendChild(start);

      function setStatus(msg, kind) {
        status.textContent = msg;
        status.classList.remove("muted", "ok", "err");
        status.classList.add(kind || "muted");
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
    },
  });
}
