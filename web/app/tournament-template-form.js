// Reusable tournament-template form. Mounted in two contexts:
//   1. Global Settings dialog → "Tournament" tab (defaults for new tournaments).
//   2. New Tournament dialog → editable; pre-filled from defaults; save = create.
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
  inputs.tournament_type = typeSelect;
  grid.appendChild(typeSelect);

  const seedsInput = addInput("seeds", "Seeds", { type: "number", min: 1 });
  grid.appendChild(seedsInput);

  function syncSeedsVisibility() {
    const isGauntlet = typeSelect.value === "gauntlet";
    seedsInput.style.display = isGauntlet ? "" : "none";
    grid.classList.toggle("ttf-grid--no-seeds", !isGauntlet);
  }
  syncSeedsVisibility();
  typeSelect.addEventListener("wa-change", syncSeedsVisibility);

  // ---- Ponder + Affinity (single row) -----------------------------------

  const switchRow = document.createElement("div");
  switchRow.className = "ttf-switch-row";

  const ponderSwitch = document.createElement("wa-switch");
  ponderSwitch.size = "small";
  ponderSwitch.dataset.key = "ponder";
  if (initialValues.ponder) ponderSwitch.setAttribute("checked", "");
  ponderSwitch.textContent = "Ponder";
  ponderSwitch.title = "Engines think on opponent's time.";
  inputs.ponder = ponderSwitch;

  const affinitySwitch = document.createElement("wa-switch");
  affinitySwitch.size = "small";
  affinitySwitch.dataset.key = "pin_affinity";
  if (initialValues.pin_affinity) affinitySwitch.setAttribute("checked", "");
  affinitySwitch.textContent = "CPU Affinity";
  affinitySwitch.title =
    "Pass -affinity to fastchess so each game-slot is bound to fixed cores. " +
    "Reduces scheduler noise; recommended for SPRT.";
  inputs.pin_affinity = affinitySwitch;

  const oversubSwitch = document.createElement("wa-switch");
  oversubSwitch.size = "small";
  oversubSwitch.dataset.key = "allow_oversubscribe";
  if (initialValues.allow_oversubscribe) oversubSwitch.setAttribute("checked", "");
  oversubSwitch.textContent = "Oversubscribe";
  oversubSwitch.title =
    "Allow CPU/RAM use to exceed the host's capacity. Resource checks " +
    "downgrade from blockers to warnings. Don't use for SPRT.";
  inputs.allow_oversubscribe = oversubSwitch;

  switchRow.append(ponderSwitch, affinitySwitch, oversubSwitch);

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
      if (on) f.removeAttribute("disabled");
      else f.setAttribute("disabled", "");
    }
    block.fields.classList.toggle("disabled", !on);
  }
  const resignFields = [resignMoves, resignScore];
  const drawFields = [drawStart, drawMoves, drawScore];
  syncEnabled(resignBlock, resignFields);
  syncEnabled(drawBlock, drawFields);
  resignBlock.sw.addEventListener("change", () => syncEnabled(resignBlock, resignFields));
  drawBlock.sw.addEventListener("change", () => syncEnabled(drawBlock, drawFields));

  container.append(grid, switchRow, adjSection);

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
    if (affinitySwitch.checked) out.pin_affinity = true;
    if (oversubSwitch.checked) out.allow_oversubscribe = true;

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

  // Returns {ok, errors:[{key,message}]}. Decorates failing inputs with
  // .ttf-invalid (cleared on each call). Caller passes engine count for
  // the gauntlet seeds check; defaults to 2 (the API minimum).
  function validate({ numEngines = 2 } = {}) {
    const errors = [];
    const push = (key, message) => errors.push({ key, message });

    const tc = (inputs.tc.value || "").trim();
    if (!tc) push("tc", "Time control is required (e.g. 10+0.1).");

    const rounds = Number(inputs.rounds.value);
    if (!Number.isFinite(rounds) || rounds < 1) {
      push("rounds", "Rounds must be ≥ 1.");
    }

    const parallel = Number(inputs.games_in_parallel.value);
    if (!Number.isFinite(parallel) || parallel < 1) {
      push("games_in_parallel", "Parallel games must be ≥ 1.");
    }

    if (typeSelect.value === "gauntlet") {
      const seeds = Number(seedsInput.value);
      if (!Number.isFinite(seeds) || seeds < 1) {
        push("seeds", "Seeds must be ≥ 1 for gauntlet.");
      } else if (seeds >= numEngines) {
        push("seeds", `Seeds must be < number of engines (${numEngines}).`);
      }
    }

    if (resignBlock.sw.checked) {
      const mc = Number(resignMoves.value);
      const sc = Number(resignScore.value);
      if (!Number.isFinite(mc) || mc < 1) push("resign.movecount", "Resign moves must be ≥ 1.");
      if (!Number.isFinite(sc) || sc < 1) push("resign.score", "Resign score must be > 0 cp.");
    }
    if (drawBlock.sw.checked) {
      const mn = Number(drawStart.value);
      const mc = Number(drawMoves.value);
      const sc = Number(drawScore.value);
      if (!Number.isFinite(mn) || mn < 1) push("draw.movenumber", "Draw start move must be ≥ 1.");
      if (!Number.isFinite(mc) || mc < 1) push("draw.movecount", "Draw moves must be ≥ 1.");
      if (!Number.isFinite(sc) || sc < 0) push("draw.score", "Draw score must be ≥ 0 cp.");
    }

    for (const el of container.querySelectorAll(".ttf-invalid")) {
      el.classList.remove("ttf-invalid");
    }
    for (const e of errors) {
      const inp = inputs[e.key];
      if (!inp) continue;
      inp.classList.add("ttf-invalid");
      // Auto-clear on the next user edit so the outline tracks intent.
      const clear = () => {
        inp.classList.remove("ttf-invalid");
        inp.removeEventListener("input", clear);
        inp.removeEventListener("wa-change", clear);
      };
      inp.addEventListener("input", clear);
      inp.addEventListener("wa-change", clear);
    }

    return { ok: errors.length === 0, errors };
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
    affinitySwitch.checked = !!values.pin_affinity;
    oversubSwitch.checked = !!values.allow_oversubscribe;

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

  return { getValues, setValues, validate };
}
