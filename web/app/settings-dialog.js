// Settings dialog: tabbed Web Awesome dialog. Apply-on-change semantics —
// every toggle / input commits to the server immediately (debounced for
// text fields). No Save button. The X just closes.

import { pickFile, showDialog, toast } from "./dialogs.js";
import { mountTournamentTemplateForm } from "./tournament-template-form.js";
import { BOARD_STYLES, DEFAULT_BOARD_STYLE, resolveBoardStyle } from "./board-styles.js";

// Persisted unit is always seconds (float). The UI picks the most natural
// display unit on load (largest unit with no fractional remainder) and
// converts back to seconds on save. UCI/cutechess/fastchess all support
// sub-second values; the wire protocol resolution is 1ms.
const DURATION_UNITS = [
  { id: "min", label: "min", toSeconds: 60 },
  { id: "sec", label: "sec", toSeconds: 1 },
  { id: "ms",  label: "ms",  toSeconds: 0.001 },
];

function pickDurationUnit(seconds) {
  if (seconds === 0) return "sec";
  // Whole minutes -> minutes (300 -> "5 min").
  if (seconds >= 60 && seconds % 60 === 0) return "min";
  // Tenth-of-a-second resolution fits "sec" (0.1, 0.5, 60.5 all stay readable).
  // Round to 1 decimal place to absorb float jitter.
  if (Math.round(seconds * 10) === seconds * 10) return "sec";
  // Otherwise ms — sub-100ms or multi-decimal values.
  return "ms";
}

function makeDurationRow({ label, seconds, minSeconds, onChange }) {
  const row = document.createElement("div");
  row.className = "settings-row";

  const lbl = document.createElement("label");
  lbl.textContent = label;

  const initialUnit = pickDurationUnit(seconds);
  let unitId = initialUnit;
  const unitDef = () => DURATION_UNITS.find((u) => u.id === unitId);

  const input = document.createElement("wa-input");
  input.size = "small";
  input.type = "number";
  input.setAttribute("autocomplete", "off");
  input.value = String(seconds / unitDef().toSeconds);
  input.min = String(minSeconds / unitDef().toSeconds);

  const unit = document.createElement("wa-select");
  unit.size = "small";
  unit.value = unitId;
  for (const u of DURATION_UNITS) {
    const opt = document.createElement("wa-option");
    opt.value = u.id;
    opt.textContent = u.label;
    unit.appendChild(opt);
  }

  function commit() {
    const raw = parseFloat(input.value);
    if (!Number.isFinite(raw) || raw < 0) return;
    const sec = Math.round(raw * unitDef().toSeconds * 1000) / 1000;  // 1ms resolution
    if (sec < minSeconds) return;
    onChange(sec);
  }

  input.addEventListener("input", commit);
  unit.addEventListener("change", () => {
    // Display-only: convert the shown value into the new unit so the
    // underlying seconds stays the same. No commit() — the value didn't
    // change; only its presentation did.
    const oldDef = DURATION_UNITS.find((u) => u.id === unitId);
    const sec = (parseFloat(input.value) || 0) * oldDef.toSeconds;
    unitId = unit.value;
    input.value = String(sec / unitDef().toSeconds);
    input.min = String(minSeconds / unitDef().toSeconds);
  });

  const inputs = document.createElement("div");
  inputs.className = "settings-duration";
  inputs.append(input, unit);
  row.append(lbl, inputs);
  return row;
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => {
      t = null;
      fn(...args);
    }, ms);
  };
}

export async function openSettingsDialog({ api, initialTab }) {
  let initial;
  let tournamentInitial;
  try {
    initial = await api("GET", "/settings");
    tournamentInitial = await api("GET", "/api/tournament-settings");
  } catch (e) {
    toast(`Couldn't load settings: ${e.message}`, { variant: "danger" });
    return;
  }

  // If board_style is changed during this dialog session, reload after
  // close so the new style takes effect on the live board. Game state
  // lives server-side and is restored via /game/sync on remount.
  const initialStyle = initial.board_style || DEFAULT_BOARD_STYLE;
  let boardStyleDirty = false;
  let boardStylePending = null;
  let boardStyleFinal = initialStyle;

  await showDialog({
    label: "Settings",
    width: "min(760px, 94vw)",
    height: "min(620px, 92vh)",
    body: (resolve, dialog) => {
      // ---- helper: PUT a partial settings update; toast on failure. ----
      const putSettings = async (patch) => {
        try {
          await api("PUT", "/settings", patch);
          window.dispatchEvent(new CustomEvent("sturddle:settings-changed"));
        } catch (e) {
          toast(`Save failed: ${e.message}`, { variant: "danger" });
        }
      };
      const putSettingsDebounced = debounce(putSettings, 400);

      const putTournamentSettings = async (patch) => {
        try {
          tournamentInitial = await api("PUT", "/api/tournament-settings", patch);
          window.dispatchEvent(new CustomEvent("sturddle:settings-changed"));
        } catch (e) {
          toast(`Save failed: ${e.message}`, { variant: "danger" });
        }
      };

      const tabs = document.createElement("wa-tab-group");
      tabs.placement = "start";

      // --- Common tab (PGN + global engine defaults) ---
      const generalTab = document.createElement("wa-tab");
      generalTab.panel = "general";
      generalTab.textContent = "Common";
      const generalPanel = document.createElement("wa-tab-panel");
      generalPanel.name = "general";

      const pgnAutosave = document.createElement("wa-switch");
      pgnAutosave.size = "small";
      pgnAutosave.checked = !!initial.pgn_autosave;
      pgnAutosave.textContent = "Save games as PGN";
      pgnAutosave.addEventListener("change", () => {
        putSettings({ pgn_autosave: pgnAutosave.checked });
      });

      generalPanel.append(pgnAutosave);

      // --- Play tab ---
      const playTab = document.createElement("wa-tab");
      playTab.panel = "play";
      playTab.textContent = "Play";
      const playPanel = document.createElement("wa-tab-panel");
      playPanel.name = "play";

      const tcInitialRow = makeDurationRow({
        label: "Initial time",
        seconds: initial.tc_initial_seconds ?? 300,
        minSeconds: 0.1,
        onChange: (sec) => putSettingsDebounced({ tc_initial_seconds: sec }),
      });
      const tcIncrementRow = makeDurationRow({
        label: "Increment per move",
        seconds: initial.tc_increment_seconds ?? 0,
        minSeconds: 0,
        onChange: (sec) => putSettingsDebounced({ tc_increment_seconds: sec }),
      });

      const humanSide = document.createElement("wa-select");
      humanSide.size = "small";
      humanSide.setAttribute("distance", "4");
      humanSide.value = initial.human_side ?? "white";
      for (const [val, label] of [["white", "White"], ["black", "Black"], ["random", "Random"]]) {
        const opt = document.createElement("wa-option");
        opt.value = val;
        opt.textContent = label;
        humanSide.append(opt);
      }
      humanSide.addEventListener("change", () => {
        putSettings({ human_side: humanSide.value });
      });
      const humanSideRow = document.createElement("div");
      humanSideRow.className = "settings-row";
      const humanSideLabel = document.createElement("label");
      humanSideLabel.textContent = "Human plays as";
      humanSideRow.append(humanSideLabel, humanSide);

      const allowTakeback = document.createElement("wa-switch");
      allowTakeback.size = "small";
      allowTakeback.checked = initial.allow_takeback !== false;
      allowTakeback.textContent = "Allow Undo (take back)";
      allowTakeback.addEventListener("change", () => {
        putSettings({ allow_takeback: allowTakeback.checked });
      });
      const takebackRow = document.createElement("div");
      takebackRow.className = "settings-row";
      takebackRow.append(allowTakeback);

      // Board style: single preset picker + live preview swatch reusing
      // cm-chessboard's CSS class + sprite so the preview matches the
      // real board exactly.
      const boardStyleRow = document.createElement("div");
      boardStyleRow.className = "settings-row";
      const boardStyleLabel = document.createElement("label");
      boardStyleLabel.textContent = "Board style";
      const boardStyleSelect = document.createElement("wa-select");
      boardStyleSelect.size = "small";
      boardStyleSelect.setAttribute("distance", "4");
      boardStyleSelect.value = initial.board_style || DEFAULT_BOARD_STYLE;
      for (const [id, def] of Object.entries(BOARD_STYLES)) {
        const opt = document.createElement("wa-option");
        opt.value = id;
        opt.textContent = def.label;
        boardStyleSelect.append(opt);
      }

      // Preview block sits below the dropdown with breathing room. Square
      // ~half the dropdown's width. The global
      // `svg.cm-chessboard { width: 100% !important }` rule (see styles.css)
      // forces full width on any SVG carrying that class, so the
      // `cm-chessboard <theme>` class goes on a fixed-size wrapper DIV
      // with the SVG nested inside.
      const previewWrap = document.createElement("div");
      previewWrap.style.marginTop = "16px";
      const previewLabel = document.createElement("label");
      previewLabel.textContent = "Board preview";
      const previewSize = 180;
      const preview = document.createElement("div");
      preview.style.width = `${previewSize}px`;
      preview.style.height = `${previewSize}px`;
      preview.style.borderRadius = "3px";
      preview.style.overflow = "hidden";
      previewWrap.append(previewLabel, preview);
      function renderPreview(styleId) {
        const def = resolveBoardStyle(styleId);
        preview.className = `cm-chessboard ${def.cssClass}`;
        preview.innerHTML = "";
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 40 40");
        svg.setAttribute("width", String(previewSize));
        svg.setAttribute("height", String(previewSize));
        svg.style.display = "block";
        const board = document.createElementNS("http://www.w3.org/2000/svg", "g");
        board.setAttribute("class", "board");
        const tile = 10;
        for (let r = 0; r < 4; r++) {
          for (let c = 0; c < 4; c++) {
            const sq = document.createElementNS("http://www.w3.org/2000/svg", "rect");
            sq.setAttribute("class", `square ${(r + c) % 2 === 0 ? "white" : "black"}`);
            sq.setAttribute("x", c * tile);
            sq.setAttribute("y", r * tile);
            sq.setAttribute("width", tile);
            sq.setAttribute("height", tile);
            board.append(sq);
          }
        }
        // Sprinkle a few pieces of each color across the mini-board so
        // theme contrast and piece-set silhouettes are both visible.
        // Each sprite piece group sits inside a 40x40 viewBox; nest a
        // sub-svg per piece with that viewBox to map it into one cell.
        const placements = [
          { piece: "bn", col: 0, row: 0 },
          { piece: "bk", col: 3, row: 1 },
          { piece: "wq", col: 1, row: 2 },
          { piece: "wp", col: 2, row: 3 },
        ];
        for (const { piece, col, row } of placements) {
          const pieceSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
          pieceSvg.setAttribute("viewBox", "0 0 40 40");
          pieceSvg.setAttribute("x", String(col * tile));
          pieceSvg.setAttribute("y", String(row * tile));
          pieceSvg.setAttribute("width", String(tile));
          pieceSvg.setAttribute("height", String(tile));
          const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
          u.setAttribute("href", `./vendor/cm-chessboard/assets/${def.piecesFile}#${piece}`);
          pieceSvg.append(u);
          board.append(pieceSvg);
        }
        svg.append(board);
        preview.append(svg);
      }
      renderPreview(initialStyle);
      boardStyleSelect.addEventListener("change", () => {
        renderPreview(boardStyleSelect.value);
        boardStyleFinal = boardStyleSelect.value;
        boardStyleDirty = boardStyleFinal !== initialStyle;
        // Track the in-flight save so we can await it before reloading on
        // close — fire-and-forget would race location.reload().
        boardStylePending = putSettings({ board_style: boardStyleFinal });
      });
      boardStyleRow.append(boardStyleLabel, boardStyleSelect, previewWrap);

      playPanel.append(tcInitialRow, tcIncrementRow, humanSideRow, takebackRow, boardStyleRow);

      // Path-row helper used by Common + Tournament tabs.
      // Layout: label on top, [path-field][Browse][Clear] on a row underneath.
      // The field is always a wa-input — editable for PGN dir, readonly
      // for paths picked via Browse only. Using a real input means long
      // values clip naturally inside the field instead of expanding the
      // row and pushing the action buttons out of column alignment.
      function pathRow(labelText, value, mode, pickerTitle, onPick, opts = {}) {
        const { hint, editable = false, placeholder } = opts;
        const row = document.createElement("div");
        row.className = "settings-tournament-path-row";
        const lbl = document.createElement("div");
        lbl.className = "settings-tournament-path-label";
        if (labelText instanceof Node) lbl.appendChild(labelText);
        else lbl.textContent = labelText;
        if (hint) {
          const h = document.createElement("span");
          h.className = "muted settings-row-hint";
          h.textContent = ` ${hint}`;
          lbl.appendChild(h);
        }
        const inner = document.createElement("div");
        inner.className = "settings-tournament-path-inner";

        const field = document.createElement("wa-input");
        field.size = "small";
        field.setAttribute("autocomplete", "off");
        field.classList.add("path-field");
        field.value = value || "";
        if (editable) {
          if (placeholder) field.placeholder = placeholder;
          field.addEventListener("input", () => {
            const v = (field.value || "").trim();
            if (v) onPick(v, { typing: true });
          });
        } else {
          field.setAttribute("readonly", "");
          field.placeholder = "(not set)";
        }

        const inner_actions = document.createElement("div");
        inner_actions.className = "settings-row-actions";
        const browse = document.createElement("wa-button");
        browse.size = "small";
        browse.textContent = "Browse…";
        browse.addEventListener("click", async () => {
          const path = await pickFile({ api, mode, title: pickerTitle });
          if (!path) return;
          field.value = path;
          onPick(path);
        });
        const clear = document.createElement("wa-button");
        clear.size = "small";
        clear.textContent = "Clear";
        if (editable) {
          // Reserve the slot so Browse aligns with the read-only rows;
          // PGN dir has a server default, so a Clear action would be a no-op.
          clear.style.visibility = "hidden";
          clear.setAttribute("aria-hidden", "true");
          clear.tabIndex = -1;
        } else {
          clear.addEventListener("click", () => {
            field.value = "";
            onPick("");
          });
        }
        inner_actions.append(browse, clear);
        inner.append(field, inner_actions);
        row.append(lbl, inner);
        return row;
      }

      // --- Engine defaults (UCI overrides + tournament book) ---
      // Lives in the Common panel so users see one place for global,
      // non-Play, non-Tournament settings.
      function makeNumRow(labelText, key, { hint } = {}) {
        const row = document.createElement("div");
        row.className = "settings-row";
        const lbl = document.createElement("label");
        lbl.textContent = labelText;
        if (hint) {
          const h = document.createElement("span");
          h.className = "muted settings-row-hint";
          h.textContent = ` ${hint}`;
          lbl.appendChild(h);
        }
        const input = document.createElement("wa-input");
        input.size = "small";
        input.type = "number";
        input.setAttribute("min", "1");
        input.setAttribute("autocomplete", "off");
        input.placeholder = "engine default";
        const cur = initial[key];
        if (cur != null) input.value = String(cur);
        input.addEventListener("input", () => {
          // Blank/0 = clear override; the API normalises both to None.
          // Non-numeric (NaN) is dropped on the floor — leave the field
          // showing what the user typed instead of silently clearing.
          const raw = (input.value || "").trim();
          if (raw === "") return putSettingsDebounced({ [key]: null });
          const n = Number(raw);
          if (Number.isFinite(n)) putSettingsDebounced({ [key]: n });
        });
        row.append(lbl, input);
        return row;
      }

      generalPanel.append(
        pathRow(
          "PGN directory",
          initial.pgn_dir ?? "",
          "directory",
          "Pick PGN directory",
          (p, ctx) => {
            // Typing → debounce; Browse → commit immediately.
            if (ctx?.typing) putSettingsDebounced({ pgn_dir: p });
            else putSettings({ pgn_dir: p });
          },
          { editable: true, placeholder: "/path/to/pgn" },
        ),
        makeNumRow("Threads", "engine_default_threads"),
        makeNumRow("Hash (MB)", "engine_default_hash_mb"),
        pathRow(
          "SyzygyPath",
          initial.engine_default_syzygy_path || "",
          "directory",
          "Pick Syzygy tablebase directory",
          (p) => putSettings({ engine_default_syzygy_path: p }),
        ),
        pathRow(
          "Opening book",
          initial.engine_default_book_path || "",
          "file",
          "Pick opening book (.epd / .pgn)",
          (p) => putSettings({ engine_default_book_path: p }),
          { hint: "(tournaments only)" },
        ),
        bookPliesAndOrderRow(),
      );

      function bookPliesAndOrderRow() {
        const row = document.createElement("div");
        row.className = "settings-row";
        const lbl = document.createElement("label");
        lbl.textContent = "Book ply depth";
        const hint = document.createElement("span");
        hint.className = "muted settings-row-hint";
        hint.textContent = " (tournaments only)";
        lbl.appendChild(hint);

        const plies = document.createElement("wa-input");
        plies.size = "small";
        plies.type = "number";
        plies.setAttribute("min", "1");
        plies.setAttribute("autocomplete", "off");
        plies.placeholder = "engine default";
        const curPlies = initial.engine_default_book_plies;
        if (curPlies != null) plies.value = String(curPlies);
        plies.addEventListener("input", () => {
          const raw = (plies.value || "").trim();
          if (raw === "") return putSettingsDebounced({ engine_default_book_plies: null });
          const n = Number(raw);
          if (Number.isFinite(n)) putSettingsDebounced({ engine_default_book_plies: n });
        });

        const order = document.createElement("wa-select");
        order.size = "small";
        order.setAttribute("distance", "4");
        order.value = initial.engine_default_book_order ?? "sequential";
        for (const [val, label] of [["sequential", "Sequential"], ["random", "Random"]]) {
          const opt = document.createElement("wa-option");
          opt.value = val;
          opt.textContent = label;
          order.append(opt);
        }
        order.addEventListener("change", () => {
          putSettings({ engine_default_book_order: order.value });
        });

        const controls = document.createElement("div");
        controls.className = "settings-row-pair";
        controls.append(plies, order);

        row.append(lbl, controls);
        return row;
      }

      // --- Tournament tab ---
      const tournamentTab = document.createElement("wa-tab");
      tournamentTab.panel = "tournament";
      tournamentTab.textContent = "Tournament";
      const tournamentPanel = document.createElement("wa-tab-panel");
      tournamentPanel.name = "tournament";

      const fastchessLabel = document.createDocumentFragment();
      const fastchessLink = document.createElement("a");
      fastchessLink.href = "https://github.com/Disservin/fastchess";
      fastchessLink.target = "_blank";
      fastchessLink.rel = "noopener noreferrer";
      fastchessLink.textContent = "Fastchess";
      fastchessLabel.append(fastchessLink, document.createTextNode(" binary"));

      tournamentPanel.append(
        pathRow(
          fastchessLabel,
          tournamentInitial.fastchess_path || tournamentInitial.fastchess_detected || "",
          "executable",
          "Pick fastchess binary",
          (p) => putTournamentSettings({ fastchess_path: p }),
        ),
        pathRow(
          "Tournaments root",
          tournamentInitial.tournaments_root || "",
          "directory",
          "Pick tournaments root",
          (p) => putTournamentSettings({ tournaments_root: p }),
        ),
      );

      const tplHost = document.createElement("div");
      tplHost.className = "settings-tournament-tpl-mount";
      const tplCtl = mountTournamentTemplateForm({
        container: tplHost,
        initialValues: tournamentInitial.default_template || {},
      });
      tournamentPanel.appendChild(tplHost);

      // Auto-save the template on input changes (debounced).
      const persistTemplate = debounce(() => {
        let template;
        try {
          template = tplCtl.getValues();
        } catch (e) {
          toast(e.message, { variant: "danger" });
          return;
        }
        putTournamentSettings({ default_template: template });
      }, 400);
      tplHost.addEventListener("input", persistTemplate);
      tplHost.addEventListener("change", persistTemplate);

      const tabByName = { general: generalTab, play: playTab, tournament: tournamentTab };
      const startTab = tabByName[initialTab] || generalTab;
      startTab.setAttribute("active", "");

      tabs.append(
        generalTab, playTab, tournamentTab,
        generalPanel, playPanel, tournamentPanel,
      );

      dialog.append(tabs);
    },
  });

  if (boardStyleDirty) {
    try { await boardStylePending; } catch {}
    // Idempotent re-PUT to guarantee the latest value is on disk before
    // reload — the change-handler PUT is fire-and-forget and could race
    // a fast dialog close.
    try { await api("PUT", "/settings", { board_style: boardStyleFinal }); } catch {}
    location.reload();
  }
}
