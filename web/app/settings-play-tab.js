// Gameplay settings tab: PGN directory, time control, side/name, and
// end-of-game rules.
// Each widget owns its own change -> putSettings; nothing cross-interacts.

import { inlineSvgIcon } from "./dialogs.js";
import { CHESS_CLOCK_SVG_INNER, CHESS_CLOCK_VIEW_BOX } from "./icons.js";
import { makeDivider, makeSection } from "./settings-ui-helpers.js";
import { SIDE } from "./chess-consts.js";

// Mirrors HVE_DIFFICULTY_MIN/MAX on the server (config.py). MAX = full
// strength; below it engine moves are softmax-sampled server-side.
const DIFFICULTY_MIN = 1;
const DIFFICULTY_MAX = 10;
const difficultyLabel = (v) =>
  v >= DIFFICULTY_MAX ? "Difficulty: max" : `Difficulty: ${v}`;

export function buildPlayTab({
  initial, putSettings, putSettingsDebounced, makeDurationRow, pathRow,
  playerNameDefault, playerNameMaxLen,
}) {
  const playTab = document.createElement("wa-tab");
  playTab.panel = "play";
  playTab.textContent = "Gameplay";
  const playPanel = document.createElement("wa-tab-panel");
  playPanel.name = "play";

  // PGN autosave is HVE-only: a non-empty pgn_dir enables it, Clear disables
  // it. Validated server-side, so PUT only on picker-commit/blur (ctx.typing
  // skips). pgn_autosave is kept in lockstep with the path.
  const pgnDirRow = pathRow(
    "PGN directory",
    initial.pgn_dir ?? "",
    "directory",
    "Pick PGN directory",
    (p, ctx) => {
      if (ctx?.typing) return;
      putSettings({ pgn_dir: p, pgn_autosave: !!p });
    },
    { editable: true, placeholder: "/path/to/pgn (empty = no autosave)" },
  );

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
  humanSide.value = initial.human_side ?? SIDE.WHITE;
  for (const [val, label] of [[SIDE.WHITE, "White"], [SIDE.BLACK, "Black"], ["random", "Random"]]) {
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
  playerNameInput.value = initial.player_name ?? "";
  playerNameInput.addEventListener("change", () => {
    const v = playerNameInput.value.trim().slice(0, playerNameMaxLen);
    putSettings({ player_name: v });
  });
  const playerNameRow = document.createElement("div");
  playerNameRow.className = "settings-row";
  const playerNameLabel = document.createElement("label");
  playerNameLabel.textContent = "Your name";
  playerNameRow.append(playerNameLabel, playerNameInput);

  // Side + name share one row -- two short controls of equal width.
  const sideNameRow = document.createElement("div");
  sideNameRow.className = "settings-pair-row";
  sideNameRow.append(humanSideRow, playerNameRow);

  const difficulty = document.createElement("wa-slider");
  difficulty.size = "small";
  difficulty.min = DIFFICULTY_MIN;
  difficulty.max = DIFFICULTY_MAX;
  difficulty.step = 1;
  difficulty.value = initial.hve_difficulty ?? DIFFICULTY_MAX;
  difficulty.setAttribute("with-markers", "");
  difficulty.setAttribute("with-tooltip", "");
  const difficultyLabelEl = document.createElement("label");
  difficultyLabelEl.textContent = difficultyLabel(difficulty.value);
  difficulty.addEventListener("input", () => {
    difficultyLabelEl.textContent = difficultyLabel(Number(difficulty.value));
  });
  difficulty.addEventListener("change", () => {
    putSettings({ hve_difficulty: Number(difficulty.value) });
  });
  const difficultyRow = document.createElement("div");
  difficultyRow.className = "settings-row settings-row-headroom";
  difficultyRow.append(difficultyLabelEl, difficulty);

  const inheritClocks = document.createElement("wa-switch");
  inheritClocks.size = "small";
  inheritClocks.checked = !!initial.inherit_pgn_clocks;
  inheritClocks.textContent = "Resume clocks from imported PGN";
  inheritClocks.addEventListener("change", () => {
    putSettings({ inherit_pgn_clocks: inheritClocks.checked });
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
  const useOpeningBook = document.createElement("wa-switch");
  useOpeningBook.size = "small";
  useOpeningBook.checked = !!initial.hve_use_opening_book;
  useOpeningBook.textContent = "Common book";
  useOpeningBook.title = "Seed each new game from the opening book set on the Common tab";
  useOpeningBook.addEventListener("change", () => {
    putSettings({ hve_use_opening_book: useOpeningBook.checked });
  });

  // Inherit PGN clocks is a view->play transition setting; Allow Undo
  // and Claim draws are end-of-game rules stacked together.
  const inheritClocksRow = document.createElement("div");
  inheritClocksRow.className = "settings-row settings-row-spaced settings-row-section-inset";
  inheritClocksRow.append(inheritClocks);
  const togglesRow = document.createElement("div");
  togglesRow.className = "settings-row settings-toggles-grid settings-row-section-inset";
  togglesRow.append(allowTakeback, autoClaimDraws, useOpeningBook);

  const tcSection = makeSection(
    inlineSvgIcon(CHESS_CLOCK_SVG_INNER, { viewBox: CHESS_CLOCK_VIEW_BOX, ariaLabel: "Time control" }),
    "Time control",
    tcInitialRow, tcIncrementRow,
  );
  const playCol = document.createElement("div");
  playCol.className = "settings-panel-col";
  playCol.append(
    pgnDirRow,
    tcSection, inheritClocksRow,
    makeDivider(),
    togglesRow,
    makeDivider(),
    sideNameRow,
    difficultyRow,
  );
  playPanel.append(playCol);

  return { tab: playTab, panel: playPanel };
}
