// Reusable tournament-template form. Mounted in three contexts:
//   1. Global Settings dialog → "Tournament" tab (defaults for new tournaments).
//   2. New Tournament dialog → editable; pre-filled from defaults; save = create.
//   3. Inspect existing tournament → read-only; renders frozen template values.
//
// Native fields cover everything Phase 1 needs: time control,
// games-in-parallel, rounds, tournament type / seeds, ponder, resign, draw.
// Hash / Threads / SyzygyPath / opening book live in the global Settings
// "Defaults" tab — applied uniformly to all engines at launch time.
// SPRT is Phase 2 work.

const TOURNAMENT_TYPES = [
  { value: "roundrobin", label: "Round robin" },
  { value: "gauntlet",   label: "Gauntlet" },
];


export function mountTournamentTemplateForm({
  container,
  initialValues = {},
  readOnly = false,
}) {
  container.innerHTML = "";
  container.classList.add("tournament-template-form");

  // ---- Core grid ---------------------------------------------------------

  const grid = document.createElement("div");
  grid.className = "ttf-grid";

  const inputs = {};

  function addInput(key, label, { type = "text", min, placeholder, defaultValue } = {}) {
    const input = document.createElement("wa-input");
    input.label = label;
    input.size = "small";
    input.type = type;
    input.dataset.key = key;
    // Suppress browser autocomplete suggestions from unrelated history.
    input.setAttribute("autocomplete", "off");
    if (min != null) input.setAttribute("min", String(min));
    if (placeholder) input.placeholder = placeholder;
    const v = initialValues[key] != null ? initialValues[key] : defaultValue;
    if (v != null) input.value = String(v);
    if (readOnly) input.setAttribute("readonly", "");
    inputs[key] = input;
    return input;
  }

  grid.append(
    addInput("tc",                "Time control",     { placeholder: "10+0.1" }),
    addInput("games_in_parallel", "Parallel games",   { type: "number", min: 1, defaultValue: 1 }),
    addInput("rounds",            "Rounds",           { type: "number", min: 1 }),
  );

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

  const seedsInput = addInput("seeds", "Seeds", { type: "number", min: 1 });
  grid.appendChild(seedsInput);

  function syncSeedsVisibility() {
    seedsInput.style.display = typeSelect.value === "gauntlet" ? "" : "none";
  }
  syncSeedsVisibility();
  typeSelect.addEventListener("wa-change", syncSeedsVisibility);

  // ---- Ponder ------------------------------------------------------------

  const ponderSwitch = document.createElement("wa-switch");
  ponderSwitch.size = "small";
  ponderSwitch.dataset.key = "ponder";
  if (initialValues.ponder) ponderSwitch.setAttribute("checked", "");
  if (readOnly) ponderSwitch.setAttribute("disabled", "");
  ponderSwitch.textContent = "Ponder (think on opponent's time)";
  inputs.ponder = ponderSwitch;

  // ---- Adjudication: Resign + Draw --------------------------------------

  // Customary fastchess adjudication defaults — used to prefill the inputs
  // when the user hasn't set anything. The on/off switch starts off; flipping
  // it on adopts these values, which the user can then override.
  const RESIGN_DEFAULTS = { movecount: 3, score: 700 };
  const DRAW_DEFAULTS   = { movenumber: 40, movecount: 8, score: 10 };

  const resign = initialValues.resign || {};
  const draw   = initialValues.draw   || {};

  const adjSection = document.createElement("div");
  adjSection.className = "ttf-adjudication";

  // Each adjudication group has its own row + an on/off switch that
  // enables/disables its inputs. The switch state is what determines
  // whether the group is included in getValues() output.
  function makeAdjGroup(label, enabled) {
    const group = document.createElement("div");
    group.className = "ttf-adj-group";
    const header = document.createElement("div");
    header.className = "ttf-adj-header";
    const sw = document.createElement("wa-switch");
    sw.size = "small";
    if (enabled) sw.setAttribute("checked", "");
    if (readOnly) sw.setAttribute("disabled", "");
    sw.textContent = label;
    header.appendChild(sw);
    const fields = document.createElement("div");
    fields.className = "ttf-adj-fields";
    group.append(header, fields);
    return { group, sw, fields };
  }

  function adjInput(label, key, { min } = {}) {
    const i = document.createElement("wa-input");
    i.label = label;
    i.size = "small";
    i.type = "number";
    i.setAttribute("autocomplete", "off");
    if (min != null) i.setAttribute("min", String(min));
    i.dataset.key = key;
    if (readOnly) i.setAttribute("readonly", "");
    inputs[key] = i;
    return i;
  }

  // Resign row
  const resignEnabled = !!(resign.movecount != null && resign.score != null);
  const resignBlock = makeAdjGroup("Resign after", resignEnabled);
  const resignMoves = adjInput("Moves", "resign.movecount", { min: 1 });
  const resignScore = adjInput("Score (cp)", "resign.score");
  resignMoves.value = String(resign.movecount ?? RESIGN_DEFAULTS.movecount);
  resignScore.value = String(resign.score ?? RESIGN_DEFAULTS.score);
  resignBlock.fields.append(resignMoves, resignScore);

  // Draw row
  const drawEnabled = !!(draw.movenumber != null && draw.movecount != null && draw.score != null);
  const drawBlock = makeAdjGroup("Draw after", drawEnabled);
  const drawStart = adjInput("From move", "draw.movenumber", { min: 1 });
  const drawMoves = adjInput("For moves", "draw.movecount", { min: 1 });
  const drawScore = adjInput("|Score| ≤ (cp)", "draw.score", { min: 0 });
  drawStart.value = String(draw.movenumber ?? DRAW_DEFAULTS.movenumber);
  drawMoves.value = String(draw.movecount ?? DRAW_DEFAULTS.movecount);
  drawScore.value = String(draw.score ?? DRAW_DEFAULTS.score);
  drawBlock.fields.append(drawStart, drawMoves, drawScore);

  adjSection.append(resignBlock.group, drawBlock.group);

  // Wire the on/off switches to enable/disable their inputs.
  function syncEnabled(block, fieldList) {
    const on = block.sw.checked;
    for (const f of fieldList) {
      if (on && !readOnly) {
        f.removeAttribute("disabled");
      } else {
        f.setAttribute("disabled", "");
      }
    }
    block.fields.classList.toggle("disabled", !on);
  }
  const resignFields = [resignMoves, resignScore];
  const drawFields = [drawStart, drawMoves, drawScore];
  syncEnabled(resignBlock, resignFields);
  syncEnabled(drawBlock, drawFields);
  resignBlock.sw.addEventListener("change", () => syncEnabled(resignBlock, resignFields));
  drawBlock.sw.addEventListener("change", () => syncEnabled(drawBlock, drawFields));

  container.append(grid, ponderSwitch, adjSection);

  // ---- Public API --------------------------------------------------------

  function getValues() {
    const out = {};

    // Core scalar fields.
    const scalars = ["tc", "games_in_parallel", "rounds"];
    for (const k of scalars) {
      const raw = inputs[k].value;
      if (raw === "" || raw == null) continue;
      out[k] = inputs[k].type === "number" ? Number(raw) : raw;
    }

    out.tournament_type = typeSelect.value;
    if (typeSelect.value === "gauntlet" && seedsInput.value) {
      out.seeds = Number(seedsInput.value);
    }
    if (ponderSwitch.checked) out.ponder = true;

    // Adjudication: only emit a sub-object when the switch is on AND
    // the required fields are present.
    if (resignBlock.sw.checked && resignMoves.value && resignScore.value) {
      out.resign = {
        movecount: Number(resignMoves.value),
        score:     Number(resignScore.value),
      };
    }
    if (
      drawBlock.sw.checked &&
      drawStart.value && drawMoves.value && drawScore.value !== ""
    ) {
      out.draw = {
        movenumber: Number(drawStart.value),
        movecount:  Number(drawMoves.value),
        score:      Number(drawScore.value),
      };
    }
    return out;
  }

  function setValues(values) {
    const scalars = ["tc", "games_in_parallel", "rounds"];
    for (const k of scalars) {
      inputs[k].value = values[k] != null ? String(values[k]) : "";
    }
    typeSelect.value = values.tournament_type || "roundrobin";
    seedsInput.value = values.seeds != null ? String(values.seeds) : "";
    syncSeedsVisibility();
    ponderSwitch.checked = !!values.ponder;

    const r = values.resign || {};
    resignMoves.value = String(r.movecount ?? RESIGN_DEFAULTS.movecount);
    resignScore.value = String(r.score ?? RESIGN_DEFAULTS.score);
    resignBlock.sw.checked = !!(r.movecount != null && r.score != null);
    syncEnabled(resignBlock, resignFields);

    const d = values.draw || {};
    drawStart.value  = String(d.movenumber ?? DRAW_DEFAULTS.movenumber);
    drawMoves.value  = String(d.movecount ?? DRAW_DEFAULTS.movecount);
    drawScore.value  = String(d.score ?? DRAW_DEFAULTS.score);
    drawBlock.sw.checked = !!(d.movenumber != null && d.movecount != null && d.score != null);
    syncEnabled(drawBlock, drawFields);
  }

  return { getValues, setValues };
}
