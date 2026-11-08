// Frost: the blur and brightness every frosted panel (styles.css) applies to
// the wallpaper behind it. One setting app-wide, saved in the browser, set
// from any frosted panel's right-click menu.

import { attachPanelMenu, RESET_LABEL } from "./panel-fade.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadJson, saveJson } from "./storage.js";
import { themeFrost } from "./theme-preset.js";
import { PCT_SUFFIX } from "./wb-utils.js";

const BLUR_VAR = "--frost-blur";
const BRIGHTNESS_VAR = "--frost-brightness";
const PX_SUFFIX = "px";
const PCT_PER_UNIT = 100;
// Computed backdrop-filter of an unfrosted element.
const NO_FILTER = "none";
// Slider specs (panel menu) keyed by their stored field.
const SLIDERS = Object.freeze({
  blur: { label: "Blur", min: 0, max: 20, step: 1, unit: PX_SUFFIX, initial: 2 },
  brightness: { label: "Brightness", min: 20, max: 120, step: 1, unit: PCT_SUFFIX, initial: 60 },
});

const inRange = (v, { min, max }) => Number.isFinite(v) && v >= min && v <= max;

// Stored values over the defaults; a missing or out-of-range field reads as default.
function load() {
  const stored = loadJson(STORAGE_KEY.FROST) ?? {};
  return Object.fromEntries(Object.entries(SLIDERS).map(([key, spec]) =>
    [key, inRange(stored[key], spec) ? stored[key] : spec.initial]));
}

const frost = load();

function apply() {
  const root = document.documentElement.style;
  root.setProperty(BLUR_VAR, `${frost.blur}${PX_SUFFIX}`);
  root.setProperty(BRIGHTNESS_VAR, String(frost.brightness / PCT_PER_UNIT));
}
apply();

const save = () => saveJson(STORAGE_KEY.FROST, frost);

// Set and save the fields given (a theme's preset); an out-of-range one is skipped.
export function setFrost(values) {
  for (const [key, spec] of Object.entries(SLIDERS)) {
    if (inRange(values[key], spec)) frost[key] = values[key];
  }
  apply();
  save();
}

const sliders = Object.entries(SLIDERS).map(([key, spec]) => ({
  ...spec,
  get: () => frost[key],
  set: (v) => { frost[key] = v; apply(); },
  save,
}));

// Reset restores the active theme's value, else the default.
function resetValue(key, spec) {
  const theme = themeFrost(key);
  return inRange(theme, spec) ? theme : spec.initial;
}
const isReset = () => Object.entries(SLIDERS).every(([key, spec]) => frost[key] === resetValue(key, spec));
const reset = Object.freeze({
  label: RESET_LABEL,
  disabled: isReset,
  run: () => {
    for (const [key, spec] of Object.entries(SLIDERS)) frost[key] = resetValue(key, spec);
    apply();
    save();
  },
});

// A frosted panel's right-click menu: the frost sliders and Reset, while the
// panel is actually frosted (styles.css: only over a wallpaper, not an empty
// move list). Returns the teardown.
export function attachFrostMenu(panel) {
  const shown = () => getComputedStyle(panel).backdropFilter !== NO_FILTER;
  return attachPanelMenu(panel, { sliders, tailActions: [reset], shown });
}
