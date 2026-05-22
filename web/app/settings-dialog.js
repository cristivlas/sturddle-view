// Settings dialog: tabbed Web Awesome dialog. Apply-on-change semantics —
// every toggle / input commits to the server immediately (debounced for
// text fields). No Save button. The X just closes.

import { apiErrorDetail, inlineSvgIcon, pickFile, showDialog, toast } from "./dialogs.js";
import { mountEngineList } from "./engines.js";
import { mountTournamentTemplateForm } from "./tournament-template-form.js";
import { BOARD_STYLES, DEFAULT_BOARD_STYLE, resolveBoardStyle } from "./board-styles.js";
import { CHESS_CLOCK_SVG_INNER, CHESS_CLOCK_VIEW_BOX } from "./icons.js";
import { mqMobile, mqNarrowDialog } from "./breakpoints.js";
import { RIBBON_SIDE_KEY } from "./ribbon-window.js";

const SETTINGS_ENGINES_COL_PCTS_KEY = "sturddle:engines:settings:colPcts3";

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

export async function openSettingsDialog({ api, initialTab, getActivePerspective, reloadPerspective }) {
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
    width: "min(690px, 94vw)",
    // Phones get the full vertical share; desktops cap at 580px.
    height: mqNarrowDialog.matches ? "92vh" : "min(580px, 92vh)",
    body: (resolve, dialog) => {
      // ---- helper: PUT a partial settings update; toast on failure. ----
      const putSettings = async (patch) => {
        try {
          await api("PUT", "/settings", patch);
          window.dispatchEvent(new CustomEvent("sturddle:settings-changed"));
        } catch (e) {
          toast(`Save failed: ${apiErrorDetail(e)}`, { variant: "danger" });
        }
      };
      const putSettingsDebounced = debounce(putSettings, 400);

      const putTournamentSettings = async (patch) => {
        try {
          tournamentInitial = await api("PUT", "/api/tournament-settings", patch);
          window.dispatchEvent(new CustomEvent("sturddle:settings-changed"));
        } catch (e) {
          toast(`Save failed: ${apiErrorDetail(e)}`, { variant: "danger" });
        }
      };

      const tabs = document.createElement("wa-tab-group");
      const isNarrow = mqNarrowDialog.matches;
      tabs.placement = isNarrow ? "top" : "start";
      tabs.classList.add("dialog-side-tabs", "settings-tabs");

      // --- Engines tab ---
      const enginesTab = document.createElement("wa-tab");
      enginesTab.panel = "engines";
      enginesTab.textContent = "Engines";
      const enginesPanel = document.createElement("wa-tab-panel");
      enginesPanel.name = "engines";
      enginesPanel.classList.add("settings-engines-panel");
      const enginesHost = document.createElement("div");
      enginesHost.className = "settings-engines-host";
      enginesPanel.appendChild(enginesHost);
      let enginesMounted = false;
      tabs.addEventListener("wa-tab-show", (ev) => {
        if (ev.detail?.name !== "engines" || enginesMounted) return;
        enginesMounted = true;
        mountEngineList(enginesHost, api, {
          colPctsKey: SETTINGS_ENGINES_COL_PCTS_KEY,
        });
      });

      // --- Common tab (PGN + global engine defaults) ---
      // PGN autosave is implicit: a non-empty pgn_dir enables it; Clear
      // disables it. The server still has a separate pgn_autosave field,
      // but the UI keeps the two in lockstep.
      const generalTab = document.createElement("wa-tab");
      generalTab.panel = "general";
      generalTab.textContent = "Common";
      const generalPanel = document.createElement("wa-tab-panel");
      generalPanel.name = "general";

      // --- Play tab ---
      const playTab = document.createElement("wa-tab");
      playTab.panel = "play";
      playTab.textContent = "Gameplay";
      const playPanel = document.createElement("wa-tab-panel");
      playPanel.name = "play";

      // --- Display tab (visual / presentation preferences) ---
      const displayTab = document.createElement("wa-tab");
      displayTab.panel = "display";
      displayTab.textContent = "Display";
      const displayPanel = document.createElement("wa-tab-panel");
      displayPanel.name = "display";

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

      const ribbonSide = document.createElement("wa-select");
      ribbonSide.size = "small";
      ribbonSide.setAttribute("distance", "4");
      ribbonSide.value = localStorage.getItem(RIBBON_SIDE_KEY) || initial.ribbon_side || "left";
      for (const [val, label] of [["left", "Left"], ["right", "Right"], ["float", "Floating"]]) {
        const opt = document.createElement("wa-option");
        opt.value = val;
        opt.textContent = label;
        ribbonSide.append(opt);
      }
      ribbonSide.addEventListener("change", () => {
        const val = ribbonSide.value;
        localStorage.setItem(RIBBON_SIDE_KEY, val);
        if (val === "float") {
          // Float is client-only -- no server PUT, so we must dispatch ourselves.
          window.dispatchEvent(new CustomEvent("sturddle:settings-changed"));
        } else {
          // putSettings dispatches sturddle:settings-changed after the PUT resolves.
          putSettings({ ribbon_side: val });
        }
      });
      const ribbonSideRow = document.createElement("div");
      ribbonSideRow.className = "settings-row";
      const ribbonSideLabel = document.createElement("label");
      ribbonSideLabel.textContent = "Controls";
      ribbonSideRow.append(ribbonSideLabel, ribbonSide);
      ribbonSideRow.hidden = mqMobile.matches;
      mqMobile.addEventListener("change", e => { ribbonSideRow.hidden = e.matches; });

      const evalPov = document.createElement("wa-select");
      evalPov.size = "small";
      evalPov.setAttribute("distance", "4");
      evalPov.value = initial.play_eval_pov ?? "white";
      for (const [val, label] of [
        ["white", "White's POV"],
        ["engine", "Engine's POV (raw UCI)"],
        ["human", "Human's POV"],
      ]) {
        const opt = document.createElement("wa-option");
        opt.value = val;
        opt.textContent = label;
        evalPov.append(opt);
      }
      evalPov.addEventListener("change", () => {
        putSettings({ play_eval_pov: evalPov.value });
      });
      const evalPovRow = document.createElement("div");
      evalPovRow.className = "settings-row";
      const evalPovLabel = document.createElement("label");
      evalPovLabel.textContent = "Eval display";
      evalPovRow.append(evalPovLabel, evalPov);

      const inheritClocks = document.createElement("wa-switch");
      inheritClocks.size = "small";
      inheritClocks.checked = !!initial.inherit_pgn_clocks;
      inheritClocks.textContent = "Resume clocks from imported PGN";
      inheritClocks.addEventListener("change", () => {
        putSettings({ inherit_pgn_clocks: inheritClocks.checked });
      });

      const showComments = document.createElement("wa-switch");
      showComments.size = "small";
      showComments.checked = initial.view_show_pgn_comments !== false;
      showComments.textContent = "PGN comments";
      showComments.title = "Display sanitized move comments in the left column while viewing a game (desktop only)";
      showComments.addEventListener("change", () => {
        putSettings({ view_show_pgn_comments: showComments.checked });
      });

      const allowTakeback = document.createElement("wa-switch");
      allowTakeback.size = "small";
      allowTakeback.checked = initial.allow_takeback !== false;
      allowTakeback.textContent = "Allow undo";
      allowTakeback.addEventListener("change", () => {
        putSettings({ allow_takeback: allowTakeback.checked });
      });
      const autoClaimDraws = document.createElement("wa-switch");
      autoClaimDraws.size = "small";
      autoClaimDraws.checked = initial.auto_claim_draws !== false;
      autoClaimDraws.textContent = "Claim draws";
      autoClaimDraws.title = "Automatically end the game on threefold repetition or 50-move rule";
      autoClaimDraws.addEventListener("change", () => {
        putSettings({ auto_claim_draws: autoClaimDraws.checked });
      });

      // Inherit PGN clocks is a view->play transition setting; Allow Undo
      // and Claim draws are end-of-game rules stacked together.
      const inheritClocksRow = document.createElement("div");
      inheritClocksRow.className = "settings-row settings-row-spaced settings-row-section-inset";
      inheritClocksRow.append(inheritClocks);
      const togglesRow = document.createElement("div");
      togglesRow.className = "settings-row settings-toggles-grid settings-row-section-inset";
      togglesRow.append(allowTakeback, autoClaimDraws);

      // Board style: single preset picker + live preview swatch reusing
      // cm-chessboard's CSS class + sprite so the preview matches the
      // real board exactly.
      const boardStyleRow = document.createElement("div");
      boardStyleRow.className = "settings-row settings-row-spaced";
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

      // Preview sits below the dropdown, full row width, two ranks tall.
      // The `cm-chessboard <theme>` class goes on the wrapper DIV; the
      // SVG inside scales via viewBox so the cells stay square as the
      // wrapper resizes with the dropdown.
      const previewWrap = document.createElement("div");
      previewWrap.style.marginTop = "16px";
      const cols = 8;
      const rows = 2;
      const tile = 10;
      const preview = document.createElement("div");
      preview.style.width = "100%";
      preview.style.aspectRatio = `${cols} / ${rows}`;
      preview.style.border = "2px solid #000";
      preview.style.borderRadius = "var(--wa-border-radius-m, 4px)";
      preview.style.overflow = "hidden";
      previewWrap.append(preview);
      function renderPreview(styleId) {
        const def = resolveBoardStyle(styleId);
        preview.className = `cm-chessboard ${def.cssClass}`;
        preview.innerHTML = "";
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", `0 0 ${cols * tile} ${rows * tile}`);
        svg.setAttribute("width", "100%");
        svg.setAttribute("height", "100%");
        svg.style.display = "block";
        const board = document.createElementNS("http://www.w3.org/2000/svg", "g");
        board.setAttribute("class", "board");
        for (let r = 0; r < rows; r++) {
          for (let c = 0; c < cols; c++) {
            const sq = document.createElementNS("http://www.w3.org/2000/svg", "rect");
            sq.setAttribute("class", `square ${(r + c) % 2 === 0 ? "white" : "black"}`);
            sq.setAttribute("x", c * tile);
            sq.setAttribute("y", r * tile);
            sq.setAttribute("width", tile);
            sq.setAttribute("height", tile);
            board.append(sq);
          }
        }
        // Sprinkle pieces across both ranks and both square colors so
        // theme contrast and piece-set silhouettes are both visible.
        const placements = [
          { piece: "bn", col: 1, row: 0 },
          { piece: "bk", col: 4, row: 0 },
          { piece: "wq", col: 3, row: 1 },
          { piece: "wp", col: 6, row: 1 },
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

      const makeDivider = () => {
        const hr = document.createElement("hr");
        hr.className = "settings-divider";
        return hr;
      };
      const makeSection = (iconEl, ariaLabel, ...children) => {
        const fs = document.createElement("fieldset");
        fs.className = "settings-section";
        const lg = document.createElement("legend");
        lg.setAttribute("aria-label", ariaLabel);
        lg.append(iconEl);
        fs.append(lg, ...children);
        return fs;
      };
      const tcSection = makeSection(
        inlineSvgIcon(CHESS_CLOCK_SVG_INNER, { viewBox: CHESS_CLOCK_VIEW_BOX, ariaLabel: "Time control" }),
        "Time control",
        tcInitialRow, tcIncrementRow,
      );
      const playCol = document.createElement("div");
      playCol.className = "settings-panel-col";
      playCol.append(
        //humanSideRow,
        //makeDivider(),
        tcSection, inheritClocksRow,
        makeDivider(),
        togglesRow,
        makeDivider(),
        humanSideRow,
      );
      playPanel.append(playCol);
      // Display tab: presentation-only preferences (no gameplay effect).
      const showCommentsDisplayRow = document.createElement("div");
      showCommentsDisplayRow.className = "settings-row";
      showCommentsDisplayRow.append(showComments);
      const displayCol = document.createElement("div");
      displayCol.className = "settings-panel-col";
      const ribbonSideDivider = makeDivider();
      ribbonSideDivider.hidden = mqMobile.matches;
      mqMobile.addEventListener("change", e => { ribbonSideDivider.hidden = e.matches; });
      displayCol.append(
        ribbonSideRow,
        ribbonSideDivider,
        evalPovRow, boardStyleRow,
        makeDivider(),
        showCommentsDisplayRow,
      );
      displayPanel.append(displayCol);

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
        const inner_actions = document.createElement("div");
        inner_actions.className = "settings-row-actions";
        const browse = document.createElement("wa-button");
        browse.size = "small";
        browse.title = "Browse…";
        browse.setAttribute("aria-label", pickerTitle || "Browse");
        const browseIcon = document.createElement("wa-icon");
        browseIcon.setAttribute("name", "folder-open");
        browse.appendChild(browseIcon);
        const clear = document.createElement("wa-button");
        clear.size = "small";
        clear.title = "Clear";
        clear.setAttribute("aria-label", `Clear ${pickerTitle || "value"}`);
        const clearIcon = document.createElement("wa-icon");
        clearIcon.setAttribute("name", "xmark");
        clear.appendChild(clearIcon);
        const syncClear = () => {
          clear.disabled = !(field.value || "").trim();
        };

        if (editable) {
          if (placeholder) field.placeholder = placeholder;
          field.addEventListener("input", () => {
            syncClear();
            onPick((field.value || "").trim(), { typing: true });
          });
          // Commit on blur / Enter -- typing-time callbacks can debounce or
          // skip; this is the "user is done editing" signal.
          field.addEventListener("change", () => {
            syncClear();
            onPick((field.value || "").trim());
          });
        } else {
          field.setAttribute("readonly", "");
          field.placeholder = "(not set)";
        }
        browse.addEventListener("click", async () => {
          const path = await pickFile({ api, mode, title: pickerTitle });
          if (!path) return;
          field.value = path;
          syncClear();
          onPick(path);
        });
        clear.addEventListener("click", () => {
          field.value = "";
          syncClear();
          onPick("");
        });
        syncClear();
        inner_actions.append(browse, clear);
        inner.append(field, inner_actions);
        row.append(lbl, inner);
        return row;
      }

      // --- Engine defaults (UCI overrides + tournament book) ---
      // Lives in the Common panel so users see one place for global,
      // non-Play, non-Tournament settings.
      function makeNumInput(labelText, key, opts = {}) {
        const { max } = opts;
        const item = document.createElement("div");
        const lbl = document.createElement("label");
        lbl.textContent = labelText;
        const input = document.createElement("wa-input");
        input.size = "small";
        input.type = "number";
        input.min = "1";
        if (max != null) input.max = String(max);
        input.autocomplete = "off";
        input.placeholder = "default";
        const cur = initial[key];
        if (cur != null) input.value = String(cur);
        input.addEventListener("input", () => {
          const raw = (input.value || "").trim();
          if (raw === "") return putSettingsDebounced({ [key]: null });
          const n = Number(raw);
          if (Number.isFinite(n)) putSettingsDebounced({ [key]: n });
        });
        item.append(lbl, input);
        return item;
      }
      function makeNumGroup(fields) {
        const row = document.createElement("div");
        row.className = "settings-num-group settings-panel-aligned";
        for (const [labelText, key, opts = {}] of fields) {
          row.append(makeNumInput(labelText, key, opts));
        }
        return row;
      }
      // Threads subgroup: bordered block holding Analysis + Play threads
      // (same UCI knob, two contexts) so the relationship is obvious;
      // Hash sits alongside as a peer with a matching border so the two
      // visually pair without padding arithmetic.
      function makeThreadsHashRow(maxThreads) {
        const row = document.createElement("div");
        row.className = "settings-num-group settings-panel-aligned";
        const threads = document.createElement("div");
        threads.className = "settings-threads-subgroup";
        const hdr = document.createElement("div");
        hdr.className = "settings-threads-subgroup-hdr";
        hdr.textContent = "Threads";
        hdr.title = "UCI Threads — sent to engines on launch";
        const inner = document.createElement("div");
        inner.className = "settings-threads-subgroup-inner";
        inner.append(
          makeNumInput("Analysis", "engine_default_analysis_threads", { max: maxThreads }),
          makeNumInput("Play", "engine_default_threads", { max: maxThreads }),
        );
        threads.append(hdr, inner);
        // Hash: just the existing input, with a border to match the
        // threads box's frame.
        const hash = makeNumInput("Hash (MB)", "engine_default_hash_mb");
        hash.classList.add("settings-hash-boxed");
        row.append(threads, hash);
        return row;
      }

      generalPanel.append(
        makeThreadsHashRow(initial.host?.logical_cores),
        pathRow(
          "PGN directory",
          initial.pgn_dir ?? "",
          "directory",
          "Pick PGN directory",
          (p, ctx) => {
            // Non-empty path implicitly enables autosave; Clear ("" path) disables it.
            // pgn_dir is validated server-side (must exist + be writable),
            // so we only PUT on picker-commit / blur -- never on every
            // keystroke. ctx.typing skips the PUT entirely.
            if (ctx?.typing) return;
            putSettings({ pgn_dir: p, pgn_autosave: !!p });
          },
          { editable: true, placeholder: "/path/to/pgn (empty = no autosave)" },
        ),
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
        row.className = "settings-row settings-panel-aligned";
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

      // --- SPRT tab ---
      const SPRT_FIELD_DEFAULTS = { elo0: 0, elo1: 10, alpha: 0.05, beta: 0.05, model: "normalized" };
      const sprtTab = document.createElement("wa-tab");
      sprtTab.panel = "sprt";
      sprtTab.textContent = "SPRT";
      const sprtPanel = document.createElement("wa-tab-panel");
      sprtPanel.name = "sprt";

      const sprtInitial = tournamentInitial.sprt_defaults || {};

      function makeSprtField(key, labelText, { step } = {}) {
        const row = document.createElement("div");
        row.className = "settings-row";
        const lab = document.createElement("label");
        lab.textContent = labelText;
        const el = document.createElement("wa-input");
        el.size = "small";
        el.type = "number";
        el.setAttribute("autocomplete", "off");
        if (step != null) el.setAttribute("step", String(step));
        const v = sprtInitial[key] != null ? sprtInitial[key] : SPRT_FIELD_DEFAULTS[key];
        el.value = String(v);
        el.dataset.key = key;
        row.append(lab, el);
        return { row, el };
      }

      const sprtElo0F  = makeSprtField("elo0",  "Elo0");
      const sprtElo1F  = makeSprtField("elo1",  "Elo1");
      const sprtAlphaF = makeSprtField("alpha", "Alpha", { step: 0.01 });
      const sprtBetaF  = makeSprtField("beta",  "Beta",  { step: 0.01 });
      const sprtElo0  = sprtElo0F.el;
      const sprtElo1  = sprtElo1F.el;
      const sprtAlpha = sprtAlphaF.el;
      const sprtBeta  = sprtBetaF.el;

      const sprtModelRow = document.createElement("div");
      sprtModelRow.className = "settings-row";
      const sprtModelLabel = document.createElement("label");
      sprtModelLabel.textContent = "Model";
      const sprtModelSelect = document.createElement("wa-select");
      sprtModelSelect.size = "small";
      sprtModelSelect.setAttribute("distance", "4");
      for (const [val, lbl] of [["normalized", "Pentanomial (logistic Elo)"], ["logistic", "Logistic (trinomial)"]]) {
        const o = document.createElement("wa-option");
        o.value = val;
        o.textContent = lbl;
        sprtModelSelect.appendChild(o);
      }
      sprtModelSelect.value = sprtInitial.model || "normalized";
      sprtModelRow.append(sprtModelLabel, sprtModelSelect);

      const sprtGrid = document.createElement("div");
      sprtGrid.className = "sprt-settings-grid";
      sprtGrid.append(sprtElo0F.row, sprtElo1F.row, sprtAlphaF.row, sprtBetaF.row, sprtModelRow);
      sprtPanel.appendChild(sprtGrid);

      function readSprtDefaults() {
        return {
          elo0:  Number(sprtElo0.value),
          elo1:  Number(sprtElo1.value),
          alpha: Number(sprtAlpha.value),
          beta:  Number(sprtBeta.value),
          model: sprtModelSelect.value || "normalized",
        };
      }

      // Validation mirrors server-side compute_sprt: elo0<elo1, 0<alpha<1,
      // 0<beta<1, all finite. Invalid fields get .sprt-invalid; persistence
      // is skipped while any field is invalid (last-valid wins, no block
      // on dialog close).
      function validateSprtDefaults() {
        const v = readSprtDefaults();
        const bad = new Set();
        if (!Number.isFinite(v.elo0)) bad.add("elo0");
        if (!Number.isFinite(v.elo1)) bad.add("elo1");
        if (Number.isFinite(v.elo0) && Number.isFinite(v.elo1) && v.elo0 >= v.elo1) {
          bad.add("elo0"); bad.add("elo1");
        }
        if (!(Number.isFinite(v.alpha) && v.alpha > 0 && v.alpha < 1)) bad.add("alpha");
        if (!(Number.isFinite(v.beta)  && v.beta  > 0 && v.beta  < 1)) bad.add("beta");
        for (const [key, el] of [["elo0", sprtElo0], ["elo1", sprtElo1], ["alpha", sprtAlpha], ["beta", sprtBeta]]) {
          el.classList.toggle("sprt-invalid", bad.has(key));
        }
        return bad.size === 0;
      }

      const persistSprt = debounce(() => {
        if (!validateSprtDefaults()) return;
        putTournamentSettings({ sprt_defaults: readSprtDefaults() });
      }, 400);

      sprtPanel.addEventListener("input", () => { validateSprtDefaults(); persistSprt(); });
      sprtPanel.addEventListener("change", () => { validateSprtDefaults(); persistSprt(); });
      validateSprtDefaults();

      const tabByName = { engines: enginesTab, general: generalTab, play: playTab, tournament: tournamentTab, sprt: sprtTab };
      const startTab = tabByName[initialTab] || generalTab;
      startTab.setAttribute("active", "");
      // Eager mount when Engines is the starting tab: defer until the
      // dialog is actually in the document so mountEngineList can measure
      // its surroundings (it reads getBoundingClientRect on the dialog
      // body to size the table-wrap). Doing it inline here would leave
      // the host detached and yield a collapsed list.
      if (startTab === enginesTab) {
        enginesMounted = true;
        dialog.addEventListener("wa-after-show", function once(ev) {
          if (ev.target !== dialog) return;
          dialog.removeEventListener("wa-after-show", once);
          mountEngineList(enginesHost, api, {
            colPctsKey: SETTINGS_ENGINES_COL_PCTS_KEY,
          });
        });
      }

      tabs.append(
        generalTab, enginesTab, playTab, displayTab, tournamentTab, sprtTab,
        generalPanel, enginesPanel, playPanel, displayPanel, tournamentPanel, sprtPanel,
      );

      dialog.append(tabs);
    },
  });

  if (boardStyleDirty) {
    try { await boardStylePending; } catch {}
    // Re-PUT guards against the fire-and-forget change-handler racing a fast close.
    try { await api("PUT", "/settings", { board_style: boardStyleFinal }); } catch {}
    if (reloadPerspective) reloadPerspective();
    else location.reload();
  }
}
