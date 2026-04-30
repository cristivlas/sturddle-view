// Settings dialog: tabbed Web Awesome dialog. Apply-on-change semantics —
// every toggle / input commits to the server immediately (debounced for
// text fields). No Save button. The X just closes.

import { showDialog, toast } from "./dialogs.js";

// Persisted unit is always seconds (float). The UI picks the most natural
// display unit on load (largest unit with no fractional remainder) and
// converts back to seconds on save. UCI/cutechess/fastchess all support
// sub-second values; the wire protocol resolution is 1ms.
const DURATION_UNITS = [
  { id: "min", label: "min", toSeconds: 60 },
  { id: "sec", label: "sec", toSeconds: 1 },
  { id: "ms",  label: "ms",  toSeconds: 0.001 },
];

function pickDurationUnit(seconds) {
  if (seconds === 0) return "sec";
  // Whole minutes -> minutes (300 -> "5 min").
  if (seconds >= 60 && seconds % 60 === 0) return "min";
  // Tenth-of-a-second resolution fits "sec" (0.1, 0.5, 60.5 all stay readable).
  // Round to 1 decimal place to absorb float jitter.
  if (Math.round(seconds * 10) === seconds * 10) return "sec";
  // Otherwise ms — sub-100ms or multi-decimal values.
  return "ms";
}

function makeDurationRow({ label, seconds, minSeconds, onChange }) {
  const row = document.createElement("div");
  row.className = "settings-row";

  const lbl = document.createElement("label");
  lbl.textContent = label;

  const initialUnit = pickDurationUnit(seconds);
  let unitId = initialUnit;
  const unitDef = () => DURATION_UNITS.find((u) => u.id === unitId);

  const input = document.createElement("wa-input");
  input.size = "small";
  input.type = "number";
  input.value = String(seconds / unitDef().toSeconds);
  input.min = String(minSeconds / unitDef().toSeconds);

  const unit = document.createElement("wa-select");
  unit.size = "small";
  unit.value = unitId;
  for (const u of DURATION_UNITS) {
    const opt = document.createElement("wa-option");
    opt.value = u.id;
    opt.textContent = u.label;
    unit.appendChild(opt);
  }

  function commit() {
    const raw = parseFloat(input.value);
    if (!Number.isFinite(raw) || raw < 0) return;
    const sec = Math.round(raw * unitDef().toSeconds * 1000) / 1000;  // 1ms resolution
    if (sec < minSeconds) return;
    onChange(sec);
  }

  input.addEventListener("input", commit);
  unit.addEventListener("change", () => {
    // Display-only: convert the shown value into the new unit so the
    // underlying seconds stays the same. No commit() — the value didn't
    // change; only its presentation did.
    const oldDef = DURATION_UNITS.find((u) => u.id === unitId);
    const sec = (parseFloat(input.value) || 0) * oldDef.toSeconds;
    unitId = unit.value;
    input.value = String(sec / unitDef().toSeconds);
    input.min = String(minSeconds / unitDef().toSeconds);
  });

  const inputs = document.createElement("div");
  inputs.className = "settings-duration";
  inputs.append(input, unit);
  row.append(lbl, inputs);
  return row;
}

function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => {
      t = null;
      fn(...args);
    }, ms);
  };
}

export async function openSettingsDialog({ api }) {
  let initial;
  try {
    initial = await api("GET", "/settings");
  } catch (e) {
    toast(`Couldn't load settings: ${e.message}`, { variant: "danger" });
    return;
  }

  return showDialog({
    label: "Settings",
    width: "min(560px, 90vw)",
    height: "min(440px, 80vh)",
    body: (resolve, dialog) => {
      // ---- helper: PUT a partial settings update; toast on failure. ----
      const putSettings = async (patch) => {
        try {
          await api("PUT", "/settings", patch);
          window.dispatchEvent(new CustomEvent("sturddle:settings-changed"));
        } catch (e) {
          toast(`Save failed: ${e.message}`, { variant: "danger" });
        }
      };
      const putSettingsDebounced = debounce(putSettings, 400);

      const tabs = document.createElement("wa-tab-group");
      tabs.placement = "start";

      // --- General tab ---
      const generalTab = document.createElement("wa-tab");
      generalTab.panel = "general";
      generalTab.textContent = "General";
      const generalPanel = document.createElement("wa-tab-panel");
      generalPanel.name = "general";

      const pgnAutosave = document.createElement("wa-switch");
      pgnAutosave.size = "small";
      pgnAutosave.checked = !!initial.pgn_autosave;
      pgnAutosave.textContent = "Save games as PGN";
      pgnAutosave.addEventListener("change", () => {
        putSettings({ pgn_autosave: pgnAutosave.checked });
      });

      const pgnDirRow = document.createElement("div");
      pgnDirRow.className = "settings-row";
      const pgnDirLabel = document.createElement("label");
      pgnDirLabel.textContent = "PGN directory";
      const pgnDir = document.createElement("wa-input");
      pgnDir.size = "small";
      pgnDir.value = initial.pgn_dir ?? "";
      pgnDir.placeholder = "/path/to/pgn";
      pgnDir.addEventListener("input", () => {
        const v = (pgnDir.value || "").trim();
        if (v) putSettingsDebounced({ pgn_dir: v });
      });
      pgnDirRow.append(pgnDirLabel, pgnDir);

      generalPanel.append(pgnAutosave, pgnDirRow);

      // --- Play tab ---
      const playTab = document.createElement("wa-tab");
      playTab.panel = "play";
      playTab.textContent = "Play";
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

      const allowTakeback = document.createElement("wa-switch");
      allowTakeback.size = "small";
      allowTakeback.checked = initial.allow_takeback !== false;
      allowTakeback.textContent = "Allow take-back";
      allowTakeback.addEventListener("change", () => {
        putSettings({ allow_takeback: allowTakeback.checked });
      });
      const takebackRow = document.createElement("div");
      takebackRow.className = "settings-row";
      takebackRow.append(allowTakeback);

      playPanel.append(tcInitialRow, tcIncrementRow, humanSideRow, takebackRow);

      tabs.append(generalTab, playTab, generalPanel, playPanel);

      dialog.append(tabs);
    },
  });
}
