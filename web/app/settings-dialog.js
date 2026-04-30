// Settings dialog: tabbed Web Awesome dialog. Apply-on-change semantics —
// every toggle / input commits to the server immediately (debounced for
// text fields). No Save button. The X just closes.

import { showDialog, toast } from "./dialogs.js";

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
      pgnAutosave.textContent = "Auto-save games as PGN";
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

      const tcInitial = document.createElement("wa-input");
      tcInitial.size = "small";
      tcInitial.type = "number";
      tcInitial.min = "1";
      tcInitial.value = String(initial.tc_initial_seconds ?? 300);
      tcInitial.addEventListener("input", () => {
        const v = parseFloat(tcInitial.value);
        if (Number.isFinite(v) && v >= 1) {
          putSettingsDebounced({ tc_initial_seconds: v });
        }
      });
      const tcInitialRow = document.createElement("div");
      tcInitialRow.className = "settings-row";
      const tcInitialLabel = document.createElement("label");
      tcInitialLabel.textContent = "Initial time (seconds)";
      tcInitialRow.append(tcInitialLabel, tcInitial);

      const tcIncrement = document.createElement("wa-input");
      tcIncrement.size = "small";
      tcIncrement.type = "number";
      tcIncrement.min = "0";
      tcIncrement.value = String(initial.tc_increment_seconds ?? 0);
      tcIncrement.addEventListener("input", () => {
        const v = parseFloat(tcIncrement.value);
        if (Number.isFinite(v) && v >= 0) {
          putSettingsDebounced({ tc_increment_seconds: v });
        }
      });
      const tcIncrementRow = document.createElement("div");
      tcIncrementRow.className = "settings-row";
      const tcIncrementLabel = document.createElement("label");
      tcIncrementLabel.textContent = "Increment per move (seconds)";
      tcIncrementRow.append(tcIncrementLabel, tcIncrement);

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
