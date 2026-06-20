// SPRT settings tab: default elo0/elo1/alpha/beta/model for new tournaments.
// Self-contained -- server I/O flows through the passed-in
// putTournamentSettings; debounce is passed so the persist cadence matches
// the rest of the dialog.

import { SPRT_DEFAULTS, sprtParamErrors } from "./tournament-events.js";

// SPRT model wire values -- mirror the server contract (compute_sprt).
const SPRT_MODEL_NORMALIZED = SPRT_DEFAULTS.model;
const SPRT_MODEL_LOGISTIC = "logistic";
const SPRT_MODEL_OPTIONS = [
  [SPRT_MODEL_NORMALIZED, "Pentanomial (logistic Elo)"],
  [SPRT_MODEL_LOGISTIC, "Logistic (trinomial)"],
];

export function buildSprtTab({ tournamentInitial, putTournamentSettings, debounce }) {
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
    const v = sprtInitial[key] != null ? sprtInitial[key] : SPRT_DEFAULTS[key];
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
  for (const [val, lbl] of SPRT_MODEL_OPTIONS) {
    const o = document.createElement("wa-option");
    o.value = val;
    o.textContent = lbl;
    sprtModelSelect.appendChild(o);
  }
  sprtModelSelect.value = sprtInitial.model || SPRT_MODEL_NORMALIZED;
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
      model: sprtModelSelect.value || SPRT_MODEL_NORMALIZED,
    };
  }

  // Invalid fields get .sprt-invalid; persistence is skipped while any field
  // is invalid (last-valid wins, no block on dialog close).
  function validateSprtDefaults() {
    const bad = sprtParamErrors(readSprtDefaults());
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

  return { tab: sprtTab, panel: sprtPanel };
}
