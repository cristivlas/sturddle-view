// Reusable tournament-template form. Mounted in three contexts:
//   1. Settings sub-area  → editable; persists as the default for new tournaments.
//   2. New Tournament dialog → editable; pre-filled from defaults; save = create.
//   3. Inspect existing tournament → read-only; renders frozen template values.
//
// Phase 1 ships the "core" field set (TC, hash, threads, games_in_parallel,
// rounds, tournament type / seeds) plus an Advanced JSON pass-through for
// anything else (SPRT, adjudication, book, tablebase). The advanced field
// keeps the spec's full template surface reachable today; individual UI
// sections for those subsystems can replace it slice-by-slice later.

const TOURNAMENT_TYPES = [
  { value: "roundrobin", label: "Round robin" },
  { value: "gauntlet",   label: "Gauntlet" },
];

const FIELD_DEFS = [
  // [key, label, type, attrs, isCore]
  ["tc",                "Time control",      "text",   { placeholder: "10+0.1" }],
  ["hash",              "Hash (MB)",          "number", { min: 1 }],
  ["threads",           "Threads",            "number", { min: 1 }],
  ["games_in_parallel", "Games in parallel",  "number", { min: 1 }],
  ["rounds",            "Rounds",             "number", { min: 1 }],
];


export function mountTournamentTemplateForm({
  container,
  initialValues = {},
  readOnly = false,
}) {
  container.innerHTML = "";
  container.classList.add("tournament-template-form");

  const grid = document.createElement("div");
  grid.className = "ttf-grid";

  const inputs = {};

  for (const [key, label, type, attrs] of FIELD_DEFS) {
    const input = document.createElement("wa-input");
    input.label = label;
    input.size = "small";
    input.type = type;
    input.dataset.key = key;
    for (const [k, v] of Object.entries(attrs || {})) input.setAttribute(k, v);
    if (key in initialValues && initialValues[key] != null) input.value = String(initialValues[key]);
    if (readOnly) input.setAttribute("readonly", "");
    inputs[key] = input;
    grid.appendChild(input);
  }

  // Tournament type: select + optional seeds. Seeds is only meaningful
  // for gauntlet, so we hide it for round-robin.
  const typeSelect = document.createElement("wa-select");
  typeSelect.label = "Tournament type";
  typeSelect.size = "small";
  typeSelect.dataset.key = "tournament_type";
  for (const opt of TOURNAMENT_TYPES) {
    const o = document.createElement("wa-option");
    o.value = opt.value;
    o.textContent = opt.label;
    typeSelect.appendChild(o);
  }
  typeSelect.value = initialValues.tournament_type || "roundrobin";
  if (readOnly) typeSelect.setAttribute("disabled", "");
  inputs.tournament_type = typeSelect;
  grid.appendChild(typeSelect);

  const seedsInput = document.createElement("wa-input");
  seedsInput.label = "Seeds";
  seedsInput.size = "small";
  seedsInput.type = "number";
  seedsInput.setAttribute("min", "1");
  seedsInput.dataset.key = "seeds";
  if (initialValues.seeds != null) seedsInput.value = String(initialValues.seeds);
  if (readOnly) seedsInput.setAttribute("readonly", "");
  inputs.seeds = seedsInput;
  grid.appendChild(seedsInput);

  function syncSeedsVisibility() {
    seedsInput.style.display = typeSelect.value === "gauntlet" ? "" : "none";
  }
  syncSeedsVisibility();
  typeSelect.addEventListener("wa-change", syncSeedsVisibility);

  // Ponder switch — keep it on its own row so the toggle is visually clear.
  const ponderRow = document.createElement("div");
  ponderRow.className = "ttf-ponder-row";
  const ponderSwitch = document.createElement("wa-switch");
  ponderSwitch.size = "small";
  ponderSwitch.dataset.key = "ponder";
  if (initialValues.ponder) ponderSwitch.setAttribute("checked", "");
  if (readOnly) ponderSwitch.setAttribute("disabled", "");
  ponderSwitch.textContent = "Ponder (think on opponent's time)";
  inputs.ponder = ponderSwitch;
  ponderRow.appendChild(ponderSwitch);

  // Advanced (JSON) textarea — escape hatch for SPRT, adjudication, book,
  // tablebase, etc. until each gets its own UI section. Pre-filled with any
  // initial keys we don't render natively.
  const knownKeys = new Set([
    ...FIELD_DEFS.map(([k]) => k),
    "tournament_type",
    "seeds",
    "ponder",
  ]);
  const advancedObj = {};
  for (const [k, v] of Object.entries(initialValues)) {
    if (!knownKeys.has(k)) advancedObj[k] = v;
  }
  const advancedDetails = document.createElement("details");
  advancedDetails.className = "ttf-advanced";
  if (Object.keys(advancedObj).length > 0) advancedDetails.open = true;
  advancedDetails.innerHTML = `
    <summary>Advanced (JSON)</summary>
    <p class="ttf-advanced-hint">SPRT, adjudication, opening book, tablebase. Merged into the template at save time. Phase 2 replaces this with per-feature sections.</p>
  `;
  const advancedTextarea = document.createElement("wa-textarea");
  advancedTextarea.size = "small";
  advancedTextarea.rows = 6;
  advancedTextarea.value = JSON.stringify(advancedObj, null, 2);
  if (readOnly) advancedTextarea.setAttribute("readonly", "");
  advancedDetails.appendChild(advancedTextarea);

  container.append(grid, ponderRow, advancedDetails);

  function getValues() {
    const out = {};
    for (const [key, , type] of FIELD_DEFS) {
      const raw = inputs[key].value;
      if (raw === "" || raw == null) continue;
      out[key] = type === "number" ? Number(raw) : raw;
    }
    out.tournament_type = typeSelect.value;
    if (typeSelect.value === "gauntlet" && seedsInput.value) {
      out.seeds = Number(seedsInput.value);
    }
    if (ponderSwitch.checked) out.ponder = true;

    // Merge advanced JSON. Invalid JSON throws — caller should toast.
    const advRaw = (advancedTextarea.value || "").trim();
    if (advRaw) {
      let parsed;
      try {
        parsed = JSON.parse(advRaw);
      } catch (e) {
        throw new Error(`Advanced JSON is invalid: ${e.message}`);
      }
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        Object.assign(out, parsed);
      } else {
        throw new Error("Advanced JSON must be an object.");
      }
    }
    return out;
  }

  function setValues(values) {
    for (const [key] of FIELD_DEFS) {
      inputs[key].value = values[key] != null ? String(values[key]) : "";
    }
    typeSelect.value = values.tournament_type || "roundrobin";
    seedsInput.value = values.seeds != null ? String(values.seeds) : "";
    syncSeedsVisibility();
    ponderSwitch.checked = !!values.ponder;
    const adv = {};
    for (const [k, v] of Object.entries(values)) {
      if (!knownKeys.has(k)) adv[k] = v;
    }
    advancedTextarea.value = JSON.stringify(adv, null, 2);
  }

  return { getValues, setValues };
}
