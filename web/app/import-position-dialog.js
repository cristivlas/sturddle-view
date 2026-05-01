// Import a position into a new HVE game from FEN or PGN text.
// Validates against the server on input change; the Start button stays
// disabled until a parse succeeds (no `*` markers, no native `required`,
// per the modern-dialog convention used elsewhere).

import { showDialog } from "./dialogs.js";

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
      let lastValid = null; // { start_fen, moves_uci, summary, side_to_move, ... }
      let validateSeq = 0;

      const wrap = document.createElement("div");
      wrap.className = "import-pos-form";

      // Format tabs.
      const tabs = document.createElement("wa-tab-group");
      tabs.placement = "top";
      tabs.innerHTML = `
        <wa-tab slot="nav" panel="fen">FEN</wa-tab>
        <wa-tab slot="nav" panel="pgn">PGN</wa-tab>
        <wa-tab-panel name="fen"></wa-tab-panel>
        <wa-tab-panel name="pgn"></wa-tab-panel>
      `;
      wrap.appendChild(tabs);

      // Single textarea reused across both tabs (their contents differ in
      // size / placeholder, but state is per-format).
      const textByFormat = { fen: "", pgn: "" };
      const textarea = document.createElement("wa-textarea");
      textarea.size = "small";
      textarea.resize = "vertical";
      textarea.rows = 4;
      textarea.placeholder =
        "Paste FEN, e.g.\nrnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
      textarea.style.fontFamily = "var(--mono-font, monospace)";
      textarea.style.width = "100%";
      // Mount the textarea inside the active panel.
      const fenPanel = tabs.querySelector('wa-tab-panel[name="fen"]');
      const pgnPanel = tabs.querySelector('wa-tab-panel[name="pgn"]');
      fenPanel.appendChild(textarea);

      // Status line: green summary or red error.
      const status = document.createElement("div");
      status.className = "import-pos-status muted";
      status.textContent = "Paste a FEN to begin.";
      wrap.appendChild(status);

      // "Play as" radio (wired but only relevant after a valid parse).
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

      // Footer.
      const cancel = document.createElement("wa-button");
      cancel.slot = "footer";
      cancel.size = "small";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => resolve(null));

      const start = document.createElement("wa-button");
      start.slot = "footer";
      start.size = "small";
      start.variant = "brand";
      start.textContent = "Start";
      start.setAttribute("disabled", "");
      start.addEventListener("click", () => {
        if (!lastValid) return;
        const playAs = playAsRow.querySelector("wa-radio-group").value;
        resolve({
          format,
          text: textByFormat[format],
          human_side: playAs,
        });
      });

      dialog.appendChild(cancel);
      dialog.appendChild(start);

      // ----- behavior -----

      function setStatus(msg, kind) {
        status.textContent = msg;
        status.classList.remove("muted", "ok", "err");
        status.classList.add(kind || "muted");
      }

      async function validate() {
        const seq = ++validateSeq;
        const text = textByFormat[format];
        if (!text.trim()) {
          lastValid = null;
          start.setAttribute("disabled", "");
          setStatus(
            format === "fen" ? "Paste a FEN to begin." : "Paste a PGN to begin.",
            "muted",
          );
          return;
        }
        try {
          const r = await api("POST", "/game/import/validate", { format, text });
          if (seq !== validateSeq) return; // a newer keystroke superseded us
          lastValid = r;
          start.removeAttribute("disabled");
          setStatus(r.summary, "ok");
        } catch (e) {
          if (seq !== validateSeq) return;
          lastValid = null;
          start.setAttribute("disabled", "");
          // api() throws "METHOD PATH -> STATUS <body>" where body is JSON
          // like {"detail":"..."}. Surface only the detail.
          let msg = String(e?.message || e);
          const match = msg.match(/->\s*\d+\s*(.*)$/);
          if (match) msg = match[1];
          try {
            const parsed = JSON.parse(msg);
            if (parsed && typeof parsed.detail === "string") msg = parsed.detail;
          } catch {
            // not JSON — keep as-is
          }
          setStatus(msg, "err");
        }
      }

      let validateTimer = null;
      textarea.addEventListener("input", (ev) => {
        textByFormat[format] = ev.target.value || "";
        clearTimeout(validateTimer);
        validateTimer = setTimeout(validate, 200);
      });

      tabs.addEventListener("wa-tab-show", (ev) => {
        const name = ev.detail?.name;
        if (name !== "fen" && name !== "pgn") return;
        format = name;
        // Move textarea into the active panel + restore that format's text.
        (name === "fen" ? fenPanel : pgnPanel).appendChild(textarea);
        textarea.value = textByFormat[name];
        textarea.placeholder =
          name === "fen"
            ? "Paste FEN, e.g.\nrnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            : 'Paste PGN, e.g.\n[Event "?"]\n[White "..."]\n[Black "..."]\n\n1. e4 e5 2. Nf3 Nc6 ...';
        textarea.rows = name === "fen" ? 3 : 8;
        validate();
      });
    },
  });
}
