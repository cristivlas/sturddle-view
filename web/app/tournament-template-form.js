// Reusable tournament-template form. Mounted in two contexts:
//   1. Global Settings dialog -> "Tournament" tab (defaults for new tournaments).
//   2. New Tournament dialog -> editable; pre-filled from defaults; save = create.
//
// Native fields: time control, games-in-parallel, rounds, tournament type /
// seeds, ponder, resign, draw, opening book (tri-state, inheriting from the
// Common book). Hash / Threads / SyzygyPath live in the global Settings
// "Common" tab -- applied uniformly to all engines at launch time.

import { basename } from "./wb-utils.js";

const TOURNAMENT_TYPES = [
  { value: "roundrobin", label: "Round robin" },
  { value: "gauntlet",   label: "Gauntlet" },
];

// Template keys carrying the per-tournament opening-book snapshot. Shared with
// the dialog + settings-tab seeding so the set is defined once.
export const BOOK_KEYS = ["book_path", "book_plies", "book_order"];

const ADJUDICATION_SUMMARY = "Adjudication";
const OPENING_BOOK_SUMMARY = "Opening book";
const OPENING_BOOK_LABEL = "Opening book";
const BOOK_PICK_TITLE = "Pick opening book (.epd / .pgn)";
const BOOK_ORDER_DEFAULT = "sequential";
const BOOK_ORDER_OPTIONS = [["sequential", "Sequential"], ["random", "Random"]];

const PONDER_TITLE = "Engines think on opponent's time.";
const AFFINITY_TITLE =
  "Pass -affinity to fastchess so each game-slot is bound to fixed cores. " +
  "Reduces scheduler noise; recommended for SPRT.";
const RESTART_TITLE =
  "Restart each engine between games (fastchess restart=on). Clears " +
  "hash/internal state for a clean start; slower than reusing processes.";


// Shared accordion group name: same value on every section disclosure makes
// wa-details close the others when one opens (native, no JS).
const DISCLOSURE_GROUP = "ttf-section";

// Accordion with exactly one panel open at all times: the first starts open.
// Opening another closes the prior via the shared `name`. Collapsing the open
// panel (clicking its own header) instead advances to the next panel (wrap-
// around), so the set is never all-collapsed.
function enforceOneOpen(items) {
  if (!items.length) return;
  items[0].open = true;
  // A panel opening in the same tick means the incoming hide is an accordion
  // swap (another panel took over) -- leave it. A hide with no such show is a
  // user self-collapse: roll to the next panel so the set is never all-closed.
  let swapping = false;
  for (const d of items) {
    d.addEventListener("wa-show", (ev) => {
      if (ev.target !== d) return;
      swapping = true;
      requestAnimationFrame(() => { swapping = false; });
    });
    d.addEventListener("wa-hide", (ev) => {
      if (ev.target !== d || swapping) return;
      const next = items[(items.indexOf(d) + 1) % items.length];
      requestAnimationFrame(() => { next.open = true; });
    });
  }
}

// Wrap a section in a collapsed disclosure with the chevron beside the label
// (icon-placement="start"), so the header reads left-to-right: > Summary.
// All share DISCLOSURE_GROUP so only one is open at a time.
function collapsedDisclosure(summary, body, className) {
  const d = document.createElement("wa-details");
  d.summary = summary;
  d.iconPlacement = "start";
  d.name = DISCLOSURE_GROUP;
  d.className = className;
  d.appendChild(body);
  return d;
}


// Opening-book tri-state. Each layer (Common -> tournament settings -> a
// tournament) either SETs a book, turns it OFF, or INHERITs the previous
// layer's choice. The empty field's placeholder disambiguates the two empty
// states, and X cycles: set -> off -> inherit -> off.
const BOOK_MODE = { SET: "set", OFF: "off", INHERIT: "inherit" };
const BOOK_OFF_PLACEHOLDER = "(no book)";
const BOOK_INHERIT_PREFIX = "inherits: ";
const BOOK_PLIES_PLACEHOLDER = "engine default";
const BOOK_X_OFF_TITLE = "No book";
const BOOK_X_INHERIT_TITLE = "Restore inherited book";

// Opening-book section: a path row (via the shared pathRow builder) plus a
// ply-depth input and an order select, with the tri-state above. Mode seeds
// from initialValues' raw encoding: book_path key present -> set/off by value;
// absent -> inherit when the caller supplies `inherited` ({path, plies,
// order}), else off. getBook() emits per `rawEmit`: raw keeps the tri-state
// encoding (inherit -> no keys) for storage; effective resolves inherit to the
// inherited values for create/edit payloads. pathRow may be null (no picker
// wired); then the section is omitted and getBook returns {}.
function buildOpeningBookSection(initialValues, pathRow, { inherited = null, rawEmit = false } = {}) {
  if (!pathRow) return { section: null, getBook: () => ({}), validateBook: () => null, pliesInput: null };
  if (inherited && !inherited.path) inherited = null;

  let mode;
  if ("book_path" in initialValues) {
    mode = initialValues.book_path ? BOOK_MODE.SET : BOOK_MODE.OFF;
  } else {
    mode = inherited ? BOOK_MODE.INHERIT : BOOK_MODE.OFF;
  }
  // OFF is only worth storing when chosen (stored "" or a user action). A
  // defaulted OFF (nothing to inherit, key never stored) must keep emitting
  // no keys, or it would bake "" into default_template and silently suppress
  // a Common book configured later.
  let userOff = "book_path" in initialValues && !initialValues.book_path;

  const plies = document.createElement("wa-input");
  plies.size = "small";
  plies.type = "number";
  plies.setAttribute("min", "1");
  plies.setAttribute("step", "1");
  plies.setAttribute("autocomplete", "off");
  plies.label = "Book ply depth";
  if (initialValues.book_plies != null) plies.value = String(initialValues.book_plies);

  const order = document.createElement("wa-select");
  order.size = "small";
  order.label = "Order";
  order.value = initialValues.book_order ?? BOOK_ORDER_DEFAULT;
  for (const [val, label] of BOOK_ORDER_OPTIONS) {
    const o = document.createElement("wa-option");
    o.value = val;
    o.textContent = label;
    order.append(o);
  }

  // X clicks and Browse picks set .value programmatically -- no native event
  // reaches the mount container, whose input/change listeners drive the
  // Settings tab's debounced persist. Announce those mutations explicitly.
  const notifyChange = () =>
    section.dispatchEvent(new Event("change", { bubbles: true }));

  const row = pathRow(
    OPENING_BOOK_LABEL, mode === BOOK_MODE.SET ? initialValues.book_path : "",
    "file", BOOK_PICK_TITLE,
    (p) => {
      mode = p ? BOOK_MODE.SET : BOOK_MODE.OFF;
      if (!p) userOff = true;
      sync();
      notifyChange();
    },
    { editable: true, onClear: () => {
      if (mode === BOOK_MODE.SET) mode = BOOK_MODE.OFF;
      else if (mode === BOOK_MODE.OFF && inherited) mode = BOOK_MODE.INHERIT;
      else mode = BOOK_MODE.OFF;
      userOff = mode === BOOK_MODE.OFF;
      row.pathField.value = "";
      sync();
      notifyChange();
    } },
  );

  // Placeholder carries the empty-field state. Depth/order stay editable with
  // any book in effect (set or inherited) -- an inherited book can still get a
  // local depth/order override; only OFF greys them out.
  function sync() {
    const inh = mode === BOOK_MODE.INHERIT;
    row.pathField.placeholder = inh
      ? BOOK_INHERIT_PREFIX + basename(inherited.path) : BOOK_OFF_PLACEHOLDER;
    const off = mode === BOOK_MODE.OFF;
    plies.disabled = off;
    order.disabled = off;
    plies.placeholder = inh && inherited.plies != null
      ? BOOK_INHERIT_PREFIX + inherited.plies : BOOK_PLIES_PLACEHOLDER;
    row.clearBtn.disabled = off && !inherited;
    row.clearBtn.title = off && inherited
      ? BOOK_X_INHERIT_TITLE : BOOK_X_OFF_TITLE;
  }
  if (mode === BOOK_MODE.INHERIT && inherited.order && !("book_order" in initialValues)) {
    order.value = inherited.order;
  }
  sync();

  // Spinning an unset depth should step from the inherited value, not from
  // empty (which the native input treats as 0/min). Prefill just before the
  // step applies -- arrow keys or a click on the spinner.
  const prefillPlies = () => {
    if (mode === BOOK_MODE.INHERIT && !plies.value && inherited.plies != null) {
      plies.value = String(inherited.plies);
    }
  };
  plies.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowUp" || ev.key === "ArrowDown") prefillPlies();
  });
  plies.addEventListener("mousedown", prefillPlies);

  const opts = document.createElement("div");
  opts.className = "ttf-book-opts";
  opts.append(plies, order);

  const section = document.createElement("div");
  section.className = "ttf-book";
  section.append(row, opts);

  const setValues = () => {
    const out = { book_path: (row.pathField.value || "").trim() };
    if (plies.value !== "" && plies.value != null) out.book_plies = Number(plies.value);
    out.book_order = order.value;
    return out;
  };

  const getBook = () => {
    if (mode === BOOK_MODE.SET) return setValues();
    // Raw storage of a defaulted (never chosen) OFF stays keyless -- see
    // userOff above.
    if (mode === BOOK_MODE.OFF) {
      return rawEmit && !userOff ? {} : { book_path: "" };
    }
    // Inherit: the path stays inherited, but depth/order may be locally
    // overridden. Raw storage keeps book_path absent and carries only actual
    // divergence (a value merely equal to the inherited one -- e.g. the
    // spinner prefill -- is not an override); effective payloads resolve the
    // path and fall back for depth.
    if (rawEmit) {
      const out = {};
      const p = plies.value !== "" && plies.value != null ? Number(plies.value) : null;
      if (p != null && p !== inherited.plies) out.book_plies = p;
      if (order.value !== (inherited.order ?? BOOK_ORDER_DEFAULT)) out.book_order = order.value;
      return out;
    }
    const out = { book_path: inherited.path };
    const p = plies.value !== "" && plies.value != null ? Number(plies.value) : inherited.plies;
    if (p != null) out.book_plies = p;
    out.book_order = order.value;
    return out;
  };

  // Ply depth (when a book is set and a value is entered) must be a positive
  // integer. Returns an error message or null; the caller decorates the input.
  const validateBook = () => {
    if (mode === BOOK_MODE.OFF) return null;
    const raw = (plies.value || "").trim();
    if (raw === "") return null;
    const n = Number(raw);
    if (!Number.isInteger(n) || n < 1) return "Book ply depth must be a positive integer.";
    return null;
  };
  return { section, getBook, validateBook, pliesInput: plies };
}


// oversized-ok: form controller -- every section registers fields into a
// shared `inputs` map that getValues/validate read back, with enable/visibility
// sync across sections (seeds, SPRT, adjudication). Just over cap after
// compaction; further shrinking means a config-driven adjudication rewrite that
// would scatter getValues/validate for no readability gain.
export function mountTournamentTemplateForm({
  container,
  initialValues = {},
  syzygyPath = "",
  pathRow = null,
  inheritedBook = null,
  rawBookEmit = false,
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

  const seedsInput = addInput("seeds", "Seeds", { type: "number", min: 1, defaultValue: 1 });
  grid.appendChild(seedsInput);

  // Cap the spinner at engines-1 (a seed needs at least one non-seed to face);
  // validate() still guards on submit. Clamp any over-max current value down.
  function setMaxSeeds(numEngines) {
    const max = Math.max(1, (numEngines || 2) - 1);
    seedsInput.setAttribute("max", String(max));
    if (Number(seedsInput.value) > max) seedsInput.value = String(max);
  }

  function syncSeedsVisibility() {
    const isGauntlet = typeSelect.value === "gauntlet";
    seedsInput.style.display = isGauntlet ? "" : "none";
    grid.classList.toggle("ttf-grid--no-seeds", !isGauntlet);
  }
  syncSeedsVisibility();
  typeSelect.addEventListener("change", syncSeedsVisibility);

  // ---- Ponder + Affinity (single row) -----------------------------------

  const switchRow = document.createElement("div");
  switchRow.className = "ttf-switch-row";

  // Switches are read directly via their const refs (getValues/validate use
  // .checked), never through the `inputs` map -- so they don't register there.
  function makeSwitch(key, label, title) {
    const sw = document.createElement("wa-switch");
    sw.size = "small";
    sw.dataset.key = key;
    if (initialValues[key]) sw.setAttribute("checked", "");
    sw.textContent = label;
    sw.title = title;
    return sw;
  }

  const ponderSwitch = makeSwitch("ponder", "Ponder", PONDER_TITLE);
  const affinitySwitch = makeSwitch("pin_affinity", "CPU Affinity", AFFINITY_TITLE);
  const restartSwitch = makeSwitch("restart_engines", "Restart engines", RESTART_TITLE);

  // SPRT on/off is driven externally by the New Tournament dialog's chip via
  // applySprt(); the form starts off (the Settings defaults tab never enables
  // it, so Rounds is always emitted there and survives in default_template).
  let sprtOn = false;
  function applySprt(on) {
    sprtOn = on;
    if (on) {
      if (typeSelect.value !== "roundrobin") {
        typeSelect.value = "roundrobin";
        syncSeedsVisibility();
      }
      typeSelect.setAttribute("disabled", "");
      inputs.rounds.setAttribute("disabled", "");
    } else {
      typeSelect.removeAttribute("disabled");
      inputs.rounds.removeAttribute("disabled");
    }
  }
  applySprt(sprtOn);

  switchRow.append(affinitySwitch, ponderSwitch, restartSwitch);

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
    return { group, sw, fields, header };
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

  // Two-sided sub-toggle in the resign header: requires both engines to agree
  // on the resign condition. Meaningless unless resign is on, so it tracks the
  // resign switch's enabled state.
  const twosidedSwitch = document.createElement("wa-switch");
  twosidedSwitch.size = "small";
  twosidedSwitch.className = "ttf-adj-subswitch";
  twosidedSwitch.dataset.key = "resign.twosided";
  twosidedSwitch.textContent = "Two-sided";
  if (resign.twosided) twosidedSwitch.setAttribute("checked", "");
  resignBlock.header.appendChild(twosidedSwitch);

  // Tablebase adjudication sub-toggle, placed beside Two-sided. Unlike
  // Two-sided it is independent of resign; it is gated solely on a global
  // SyzygyPath being configured (passed to fastchess as -tb <path>, all
  // other -tb knobs left at fastchess defaults).
  const tbSwitch = document.createElement("wa-switch");
  tbSwitch.size = "small";
  tbSwitch.className = "ttf-adj-subswitch";
  tbSwitch.dataset.key = "tb_adjudication";
  tbSwitch.textContent = "Tablebase adjudication";
  if (!syzygyPath) tbSwitch.setAttribute("disabled", "");
  if (syzygyPath && initialValues.tb_adjudication) tbSwitch.setAttribute("checked", "");
  resignBlock.header.appendChild(tbSwitch);

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
  }
  const resignFields = [resignMoves, resignScore, twosidedSwitch];
  const drawFields = [drawStart, drawMoves, drawScore];
  syncEnabled(resignBlock, resignFields);
  syncEnabled(drawBlock, drawFields);
  resignBlock.sw.addEventListener("change", () => syncEnabled(resignBlock, resignFields));
  drawBlock.sw.addEventListener("change", () => syncEnabled(drawBlock, drawFields));

  // Collapsible sections live in a fixed-height area (so expanding never grows
  // the dialog) and behave as an accordion with EXACTLY one open at all times.
  const adjDetails = collapsedDisclosure(ADJUDICATION_SUMMARY, adjSection, "ttf-details");
  const { section: bookSection, getBook, validateBook, pliesInput } =
    buildOpeningBookSection(initialValues, pathRow, { inherited: inheritedBook, rawEmit: rawBookEmit });
  if (pliesInput) inputs["book_plies"] = pliesInput;
  const bookDetails = bookSection
    ? collapsedDisclosure(OPENING_BOOK_SUMMARY, bookSection, "ttf-details") : null;

  const sections = document.createElement("div");
  sections.className = "ttf-sections";
  sections.appendChild(adjDetails);
  if (bookDetails) sections.appendChild(bookDetails);
  container.append(grid, switchRow, sections);
  enforceOneOpen([adjDetails, bookDetails].filter(Boolean));

  // ---- Public API --------------------------------------------------------

  function getValues() {
    const out = {};

    // rounds omitted when SPRT is on (fastchess self-terminates -- a stored
    // rounds would be a lie). The Settings defaults tab never enters SPRT
    // mode, so its default_template always keeps rounds for prefill.
    const scalars = sprtOn ? ["tc", "games_in_parallel"] : ["tc", "games_in_parallel", "rounds"];
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
    if (restartSwitch.checked) out.restart_engines = true;

    // Adjudication: only emit a sub-object when the switch is on AND
    // the required fields are present.
    if (resignBlock.sw.checked && resignMoves.value && resignScore.value) {
      out.resign = {
        movecount: Number(resignMoves.value),
        score:     Number(resignScore.value),
      };
      if (twosidedSwitch.checked) out.resign.twosided = true;
    }
    if (syzygyPath && tbSwitch.checked) out.tb_adjudication = true;
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
    Object.assign(out, getBook());
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

    if (!sprtOn) {
      const rounds = Number(inputs.rounds.value);
      if (!Number.isFinite(rounds) || rounds < 1) {
        push("rounds", "Rounds must be >= 1.");
      }
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
    const bookErr = validateBook();
    if (bookErr) push("book_plies", bookErr);
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
        inp.removeEventListener("change", clear);
      };
      inp.addEventListener("input", clear);
      inp.addEventListener("change", clear);
    }

    return { ok: errors.length === 0, errors };
  }

  return { getValues, validate, applySprt, setMaxSeeds };
}
