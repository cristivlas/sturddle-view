// SPRT chip button + params popup for the New/Edit Tournament dialog. Replaces
// the old global SPRT settings tab and the per-form on/off switch: one control
// that carries both "is SPRT on" and its elo0/elo1/alpha/beta/model params.
//
// The button reflects state -- outline (off), filled brand + checkmark (on),
// greyed (disabled). Disabled when the roster isn't exactly 2 engines. Turning
// SPRT on forces round-robin and self-termination via the host form (onChange).

import { showDialog } from "./dialogs.js";
import { SPRT_DEFAULTS, sprtParamErrors } from "./tournament-events.js";

const SPRT_MODEL_LOGISTIC = "logistic";
const SPRT_MODEL_OPTIONS = [
  [SPRT_DEFAULTS.model, "Pentanomial (logistic Elo)"],
  [SPRT_MODEL_LOGISTIC, "Logistic (trinomial)"],
];
// Short model name (no parenthetical) for the info dialog. "pentanomial" is
// the server-side alias for "normalized"; both map to the pentanomial label.
// Falls back to the raw value for any unknown model.
export function sprtModelLabel(model) {
  const key = model === "pentanomial" ? SPRT_DEFAULTS.model : model;
  const found = SPRT_MODEL_OPTIONS.find(([val]) => val === key);
  return found ? found[1].replace(/\s*\(.*\)$/, "") : (model ?? "");
}
const PARAM_KEYS = ["elo0", "elo1", "alpha", "beta"];
const DISABLED_TITLE = "SPRT requires exactly 2 engines.";
const ENABLED_TITLE = "Sequential Probability Ratio Test: edit params / toggle.";

// One numeric param field mirroring the dialog's settings-row layout.
function makeField(key, labelText, value, step) {
  const row = document.createElement("div");
  row.className = "settings-row";
  const lab = document.createElement("label");
  lab.textContent = labelText;
  const el = document.createElement("wa-input");
  el.size = "small";
  el.type = "number";
  el.setAttribute("autocomplete", "off");
  if (step != null) el.setAttribute("step", String(step));
  el.value = String(value);
  el.dataset.key = key;
  row.append(lab, el);
  return { row, el };
}

// Open the params popup (gear). Edits params only; on/off lives on the chip
// toggle. Commits on close, resolving the latest params (dormant until the
// toggle turns SPRT on). Invalid fields just get flagged; Create re-validates.
function openParamsPopup({ params }) {
  const result = { params: { ...params } };
  return showDialog({
    label: "SPRT",
    width: "min(420px, 94vw)",
    defaultValue: result,
    body: (resolve, dialog) => {
      dialog.classList.add("sprt-params-dialog");

      const fields = PARAM_KEYS.map((k) =>
        makeField(k, k.charAt(0).toUpperCase() + k.slice(1),
          params[k], k === "alpha" || k === "beta" ? 0.01 : null));

      const modelRow = document.createElement("div");
      modelRow.className = "settings-row";
      const modelLabel = document.createElement("label");
      modelLabel.textContent = "Model";
      const modelSelect = document.createElement("wa-select");
      modelSelect.size = "small";
      modelSelect.setAttribute("distance", "4");
      for (const [val, lbl] of SPRT_MODEL_OPTIONS) {
        const o = document.createElement("wa-option");
        o.value = val;
        o.textContent = lbl;
        modelSelect.appendChild(o);
      }
      modelSelect.value = params.model || SPRT_DEFAULTS.model;
      modelRow.append(modelLabel, modelSelect);

      const grid = document.createElement("div");
      grid.className = "sprt-settings-grid";
      grid.append(...fields.map((f) => f.row), modelRow);

      function read() {
        const out = { model: modelSelect.value || SPRT_DEFAULTS.model };
        for (const f of fields) out[f.el.dataset.key] = Number(f.el.value);
        return out;
      }
      // Live commit into result; invalid fields flagged but not blocked.
      function sync() {
        const p = read();
        const bad = sprtParamErrors(p);
        for (const f of fields) f.el.classList.toggle("sprt-invalid", bad.has(f.el.dataset.key));
        result.params = p;
      }
      grid.addEventListener("input", sync);
      grid.addEventListener("change", sync);
      sync();

      dialog.append(grid);
    },
  });
}

// Mount the SPRT control: a toggle (on/off) + a gear that edits the params.
// onChange(on) fires whenever the on-state changes so the host form can force
// round-robin / drop the Rounds field. Disabled (greyed) unless 2 engines.
export function mountSprtButton({ host, initialSprt, sprtDefaults, onChange }) {
  let on = !!initialSprt;
  let params = { ...SPRT_DEFAULTS, ...(sprtDefaults || {}),
    ...(initialSprt && typeof initialSprt === "object" ? initialSprt : {}) };
  let available = false;

  host.classList.add("sprt-control");

  const toggle = document.createElement("wa-switch");
  toggle.size = "small";
  toggle.className = "sprt-toggle";
  toggle.textContent = "SPRT";

  const gearBtn = document.createElement("wa-button");
  gearBtn.size = "small";
  gearBtn.className = "sprt-gear icon-only";
  gearBtn.setAttribute("appearance", "plain");
  gearBtn.innerHTML = `<wa-icon name="gear"></wa-icon>`;

  function render() {
    toggle.disabled = !available;
    gearBtn.disabled = !available;
    toggle.checked = on;
    toggle.title = available ? ENABLED_TITLE : DISABLED_TITLE;
  }

  function setOn(next) {
    const changed = next !== on;
    on = next;
    render();
    if (changed) onChange?.(on);
  }

  let popupOpen = false;
  toggle.addEventListener("change", () => setOn(toggle.checked));
  gearBtn.addEventListener("click", async () => {
    if (!available || popupOpen) return;
    popupOpen = true;
    try {
      const res = await openParamsPopup({ params });
      params = res.params;
    } finally {
      popupOpen = false;
    }
  });

  host.append(toggle, gearBtn);
  render();

  return {
    isOn: () => on,
    // The params object to write into template.sprt (only when on).
    getParams: () => ({ ...params }),
    setAvailable: (next) => {
      available = !!next;
      if (!available && on) setOn(false);
      render();
    },
  };
}
