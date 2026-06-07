// Play (Gameplay) and Display settings tabs. Built together because they
// share several widgets/helpers (dividers, sections, the mobile media query)
// and are presented as a pair.
//
// Board-style is special: changing it must reload the app on dialog close so
// the live board picks up the new theme. That dirty-tracking lives in the
// dialog (it owns the close/reload), so the change handler calls back via
// onBoardStyleChange(styleId) -> Promise (the in-flight PUT) and the dialog
// records it.

import { RIBBON_SIDE_KEY } from "./ribbon-window.js";
import { APP_EVT } from "./app-events.js";
import { BOARD_STYLES, DEFAULT_BOARD_STYLE, resolveBoardStyle } from "./board-styles.js";
import { inlineSvgIcon } from "./dialogs.js";
import { CHESS_CLOCK_SVG_INNER, CHESS_CLOCK_VIEW_BOX } from "./icons.js";
import { mqMobile } from "./breakpoints.js";

export function buildPlayDisplayTabs({
  initial, putSettings, putSettingsDebounced, makeDurationRow,
  initialStyle, onBoardStyleChange,
  playerNameDefault, playerNameKey, playerNameMaxLen,
}) {
  const playTab = document.createElement("wa-tab");
  playTab.panel = "play";
  playTab.textContent = "Gameplay";
  const playPanel = document.createElement("wa-tab-panel");
  playPanel.name = "play";

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

  const playerNameInput = document.createElement("wa-input");
  playerNameInput.size = "small";
  playerNameInput.placeholder = playerNameDefault;
  playerNameInput.maxlength = playerNameMaxLen;
  playerNameInput.value = localStorage.getItem(playerNameKey) || "";
  playerNameInput.addEventListener("change", () => {
    const v = playerNameInput.value.trim().slice(0, playerNameMaxLen);
    if (v) localStorage.setItem(playerNameKey, v);
    else localStorage.removeItem(playerNameKey);
  });
  const playerNameRow = document.createElement("div");
  playerNameRow.className = "settings-row settings-row-spaced";
  const playerNameLabel = document.createElement("label");
  playerNameLabel.textContent = "Your name";
  playerNameRow.append(playerNameLabel, playerNameInput);

  const ribbonSide = document.createElement("wa-select");
  ribbonSide.size = "small";
  ribbonSide.setAttribute("distance", "4");
  ribbonSide.value = localStorage.getItem(RIBBON_SIDE_KEY) || initial.ribbon_side || "left";
  for (const [val, label] of [["left", "Left Ribbon"], ["right", "Right Ribbon"], ["float", "Floating"]]) {
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
      window.dispatchEvent(new CustomEvent(APP_EVT.SETTINGS_CHANGED));
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
    // Dialog owns the dirty-tracking + reload-on-close; hand it the new
    // style and the in-flight PUT so it can await before reloading.
    onBoardStyleChange(boardStyleSelect.value);
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
    tcSection, inheritClocksRow,
    makeDivider(),
    togglesRow,
    makeDivider(),
    humanSideRow,
    playerNameRow,
  );
  playPanel.append(playCol);

  // Display tab: presentation-only preferences (no gameplay effect).
  const showCommentsDisplayRow = document.createElement("div");
  // PGN comments render in the left column, which is hidden on mobile;
  // hide the toggle there too (desktop-only).
  showCommentsDisplayRow.className = "settings-row desktop-only";
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

  return { playTab, playPanel, displayTab, displayPanel };
}
