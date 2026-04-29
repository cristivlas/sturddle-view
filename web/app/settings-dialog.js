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

      // --- Engines tab ---
      const enginesTab = document.createElement("wa-tab");
      enginesTab.panel = "engines";
      enginesTab.textContent = "Engines";
      const enginesPanel = document.createElement("wa-tab-panel");
      enginesPanel.name = "engines";
      const enginesContainer = document.createElement("div");
      enginesContainer.className = "engines-host";
      enginesPanel.append(enginesContainer);

      tabs.append(generalTab, enginesTab, generalPanel, enginesPanel);

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
        };
        try {
          await api("PUT", "/settings", payload);
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
