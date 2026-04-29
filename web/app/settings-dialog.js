// Settings dialog: tabbed Web Awesome dialog. One tab per category.
// Phase 1: only General (PGN). Engines/Tablebases/Theme tabs come next.

import { showDialog, toast } from "./dialogs.js";
import { mountEngines } from "./engines.js";

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
    width: "min(900px, 80vw)",
    body: (resolve, dialog) => {
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

      const pgnDirRow = document.createElement("div");
      pgnDirRow.className = "settings-row";
      const pgnDirLabel = document.createElement("label");
      pgnDirLabel.textContent = "PGN directory";
      const pgnDir = document.createElement("wa-input");
      pgnDir.size = "small";
      pgnDir.value = initial.pgn_dir ?? "";
      pgnDir.placeholder = "/path/to/pgn";
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
      const tcIncrementRow = document.createElement("div");
      tcIncrementRow.className = "settings-row";
      const tcIncrementLabel = document.createElement("label");
      tcIncrementLabel.textContent = "Increment per move (seconds)";
      tcIncrementRow.append(tcIncrementLabel, tcIncrement);

      const humanSide = document.createElement("wa-select");
      humanSide.size = "small";
      humanSide.value = initial.human_side ?? "white";
      for (const [val, label] of [["white", "White"], ["black", "Black"], ["random", "Random"]]) {
        const opt = document.createElement("wa-option");
        opt.value = val;
        opt.textContent = label;
        humanSide.append(opt);
      }
      const humanSideRow = document.createElement("div");
      humanSideRow.className = "settings-row";
      const humanSideLabel = document.createElement("label");
      humanSideLabel.textContent = "Human plays as";
      humanSideRow.append(humanSideLabel, humanSide);

      const allowTakeback = document.createElement("wa-switch");
      allowTakeback.size = "small";
      allowTakeback.checked = initial.allow_takeback !== false;
      allowTakeback.textContent = "Allow take-back";
      const takebackRow = document.createElement("div");
      takebackRow.className = "settings-row";
      takebackRow.append(allowTakeback);

      playPanel.append(tcInitialRow, tcIncrementRow, humanSideRow, takebackRow);

      // --- Engines tab ---
      const enginesTab = document.createElement("wa-tab");
      enginesTab.panel = "engines";
      enginesTab.textContent = "Engines";
      const enginesPanel = document.createElement("wa-tab-panel");
      enginesPanel.name = "engines";
      const enginesContainer = document.createElement("div");
      enginesContainer.className = "engines-host";
      enginesPanel.append(enginesContainer);

      tabs.append(generalTab, playTab, enginesTab, generalPanel, playPanel, enginesPanel);

      // Mount engines after the tab panel is in the DOM tree.
      requestAnimationFrame(() => {
        mountEngines({
          container: enginesContainer,
          api,
          onError: (msg) => toast(msg, { variant: "danger" }),
        });
      });

      // Footer
      const cancel = document.createElement("wa-button");
      cancel.slot = "footer";
      cancel.size = "small";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => resolve(false));

      const save = document.createElement("wa-button");
      save.slot = "footer";
      save.size = "small";
      save.variant = "brand";
      save.textContent = "Save";
      save.addEventListener("click", async () => {
        const payload = {
          pgn_autosave: pgnAutosave.checked,
          pgn_dir: (pgnDir.value || "").trim() || null,
          tc_initial_seconds: parseFloat(tcInitial.value) || 300,
          tc_increment_seconds: parseFloat(tcIncrement.value) || 0,
          human_side: humanSide.value,
          allow_takeback: allowTakeback.checked,
        };
        try {
          await api("PUT", "/settings", payload);
          window.dispatchEvent(new CustomEvent("sturddle:settings-changed"));
          toast("Settings saved", { variant: "success" });
          resolve(true);
        } catch (e) {
          toast(`Save failed: ${e.message}`, { variant: "danger" });
        }
      });

      dialog.append(tabs, cancel, save);
    },
  });
}
