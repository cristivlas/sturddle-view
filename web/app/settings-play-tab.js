// Gameplay settings tab: time control, side/name, and end-of-game rules.
// Each widget owns its own change -> putSettings; nothing cross-interacts.

import { inlineSvgIcon } from "./dialogs.js";
import { CHESS_CLOCK_SVG_INNER, CHESS_CLOCK_VIEW_BOX } from "./icons.js";
import { makeDivider, makeSection } from "./settings-ui-helpers.js";
import { loadRaw, saveRaw } from "./storage.js";

export function buildPlayTab({
  initial, putSettings, putSettingsDebounced, makeDurationRow,
  playerNameDefault, playerNameKey, playerNameMaxLen,
}) {
  const playTab = document.createElement("wa-tab");
  playTab.panel = "play";
  playTab.textContent = "Gameplay";
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

  const playerNameInput = document.createElement("wa-input");
  playerNameInput.size = "small";
  playerNameInput.placeholder = playerNameDefault;
  playerNameInput.maxlength = playerNameMaxLen;
  playerNameInput.value = loadRaw(playerNameKey, "");
  playerNameInput.addEventListener("change", () => {
    const v = playerNameInput.value.trim().slice(0, playerNameMaxLen);
    saveRaw(playerNameKey, v || null);
  });
  const playerNameRow = document.createElement("div");
  playerNameRow.className = "settings-row settings-row-spaced";
  const playerNameLabel = document.createElement("label");
  playerNameLabel.textContent = "Your name";
  playerNameRow.append(playerNameLabel, playerNameInput);

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

  // Inherit PGN clocks is a view->play transition setting; Allow Undo
  // and Claim draws are end-of-game rules stacked together.
  const inheritClocksRow = document.createElement("div");
  inheritClocksRow.className = "settings-row settings-row-spaced settings-row-section-inset";
  inheritClocksRow.append(inheritClocks);
  const togglesRow = document.createElement("div");
  togglesRow.className = "settings-row settings-toggles-grid settings-row-section-inset";
  togglesRow.append(allowTakeback, autoClaimDraws);

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

  return { tab: playTab, panel: playPanel };
}
