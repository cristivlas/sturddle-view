// Tournament settings tab: fastchess binary path, tournaments root, and the
// default-template form. pathRow is shared with the Common tab, so it's
// passed in rather than duplicated. Server I/O flows through
// putTournamentSettings.

import { mountTournamentTemplateForm } from "./tournament-template-form.js";
import { toast } from "./dialogs.js";

const TOURNAMENT_TAB = "tournament";
const TEMPLATE_PUT_DEBOUNCE_MS = 400;
// The server refuses a root change mid-run (it would lose the running
// tournament); the UI locks the row to match.
const ROOT_LOCKED_TITLE = "Stop the running tournament to change the folder";

export function buildTournamentTab({ tournamentInitial, putTournamentSettings, pathRow, debounce }) {
  const tournamentTab = document.createElement("wa-tab");
  tournamentTab.panel = TOURNAMENT_TAB;
  tournamentTab.textContent = "Tournament";
  const tournamentPanel = document.createElement("wa-tab-panel");
  tournamentPanel.name = TOURNAMENT_TAB;

  const fastchessLabel = document.createDocumentFragment();
  const fastchessLink = document.createElement("a");
  fastchessLink.href = "https://github.com/Disservin/fastchess";
  fastchessLink.target = "_blank";
  fastchessLink.rel = "noopener noreferrer";
  fastchessLink.textContent = "Fastchess";
  fastchessLabel.append(fastchessLink, document.createTextNode(" binary"));

  const rootRow = pathRow(
    "Tournaments root",
    tournamentInitial.tournaments_root || "",
    "directory",
    "Pick tournaments root",
    (p) => putTournamentSettings({ tournaments_root: p }),
  );
  if (tournamentInitial.tournament_running) {
    rootRow.browseBtn.disabled = true;
    rootRow.clearBtn.disabled = true;
    rootRow.title = ROOT_LOCKED_TITLE;
  }

  tournamentPanel.append(
    pathRow(
      fastchessLabel,
      tournamentInitial.fastchess_path || tournamentInitial.fastchess_detected || "",
      "executable",
      "Pick fastchess binary",
      (p) => putTournamentSettings({ fastchess_path: p }),
    ),
    rootRow,
  );

  const tplHost = document.createElement("div");
  tplHost.className = "settings-tournament-tpl-mount";
  // Book tri-state: this tab inherits from the Common book; raw emit keeps
  // the inherit state as absent keys in the stored default_template.
  const tplCtl = mountTournamentTemplateForm({
    container: tplHost,
    initialValues: tournamentInitial.default_template || {},
    syzygyPath: tournamentInitial.engine_default_syzygy_path || "",
    pathRow,
    inheritedBook: {
      path: tournamentInitial.engine_default_book_path,
      plies: tournamentInitial.engine_default_book_plies,
      order: tournamentInitial.engine_default_book_order,
    },
    rawBookEmit: true,
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
    putTournamentSettings({ default_template: template });
  }, TEMPLATE_PUT_DEBOUNCE_MS);
  tplHost.addEventListener("input", persistTemplate);
  tplHost.addEventListener("change", persistTemplate);

  return { tab: tournamentTab, panel: tournamentPanel };
}
