// Tournament settings tab: fastchess binary path, tournaments root, and the
// default-template form. pathRow is shared with the Common tab, so it's
// passed in rather than duplicated. Server I/O flows through
// putTournamentSettings.

import { BOOK_KEYS, mountTournamentTemplateForm } from "./tournament-template-form.js";
import { toast } from "./dialogs.js";

// The default-template editor stores tournament defaults only. Book fields left
// empty must NOT be baked into default_template as empty values -- an empty
// book_path there would suppress the Common-book fallback that create applies.
// Drop empty/absent book keys so the create-time cascade (default_template ->
// Common) stays intact; a book explicitly set here is kept and wins.
function stripEmptyBook(template) {
  const out = { ...template };
  for (const k of BOOK_KEYS) {
    if (out[k] == null || out[k] === "") delete out[k];
  }
  return out;
}

export function buildTournamentTab({ tournamentInitial, putTournamentSettings, pathRow, debounce }) {
  const tournamentTab = document.createElement("wa-tab");
  tournamentTab.panel = "tournament";
  tournamentTab.textContent = "Tournament";
  const tournamentPanel = document.createElement("wa-tab-panel");
  tournamentPanel.name = "tournament";

  const fastchessLabel = document.createDocumentFragment();
  const fastchessLink = document.createElement("a");
  fastchessLink.href = "https://github.com/Disservin/fastchess";
  fastchessLink.target = "_blank";
  fastchessLink.rel = "noopener noreferrer";
  fastchessLink.textContent = "Fastchess";
  fastchessLabel.append(fastchessLink, document.createTextNode(" binary"));

  tournamentPanel.append(
    pathRow(
      fastchessLabel,
      tournamentInitial.fastchess_path || tournamentInitial.fastchess_detected || "",
      "executable",
      "Pick fastchess binary",
      (p) => putTournamentSettings({ fastchess_path: p }),
    ),
    pathRow(
      "Tournaments root",
      tournamentInitial.tournaments_root || "",
      "directory",
      "Pick tournaments root",
      (p) => putTournamentSettings({ tournaments_root: p }),
    ),
  );

  const tplHost = document.createElement("div");
  tplHost.className = "settings-tournament-tpl-mount";
  const tplCtl = mountTournamentTemplateForm({
    container: tplHost,
    initialValues: tournamentInitial.default_template || {},
    syzygyPath: tournamentInitial.engine_default_syzygy_path || "",
    pathRow,
  });
  tournamentPanel.appendChild(tplHost);

  // Auto-save the template on input changes (debounced).
  const persistTemplate = debounce(() => {
    let template;
    try {
      template = tplCtl.getValues();
    } catch (e) {
      toast(e.message, { variant: "danger" });
      return;
    }
    putTournamentSettings({ default_template: stripEmptyBook(template) });
  }, 400);
  tplHost.addEventListener("input", persistTemplate);
  tplHost.addEventListener("change", persistTemplate);

  return { tab: tournamentTab, panel: tournamentPanel };
}
