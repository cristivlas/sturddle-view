// Tournament settings tab: fastchess binary path, tournaments root, and the
// default-template form. pathRow is shared with the Common tab, so it's
// passed in rather than duplicated. Server I/O flows through
// putTournamentSettings.

import { mountTournamentTemplateForm } from "./tournament-template-form.js";
import { confirm, toast } from "./dialogs.js";
import { APP_EVT } from "./app-events.js";

const TOURNAMENT_TAB = "tournament";
const TEMPLATE_PUT_DEBOUNCE_MS = 400;
// The server refuses a root change mid-run (it would lose the running
// tournament); the UI locks the row to match.
const ROOT_LOCKED_TITLE = "Stop the running tournament to change the folder";

function rootSwitchMessage(count) {
  const noun = count === 1 ? "tournament" : "tournaments";
  return `${count} ${noun} in the current folder won't be listed until you switch back.`;
}

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

  // The folder the lists read from, its tournament count, and whether it is
  // the default (then Clear has nothing to clear and stays disabled).
  const rootFrom = (s) => ({
    path: s.tournaments_root || "",
    count: s.tournaments_in_root || 0,
    isDefault: !!s.tournaments_root_is_default,
  });
  let root = rootFrom(tournamentInitial);
  const showRoot = () => {
    rootRow.setValue(root.path);
    if (root.isDefault) rootRow.clearBtn.disabled = true;
  };
  // Switching away hides the current folder's tournaments: confirm first,
  // revert the field on Cancel or a failed save.
  const changeRoot = async (p) => {
    if (p === root.path) return;
    if (root.count > 0 && !(await confirm({ message: rootSwitchMessage(root.count), okLabel: "Switch" }))) {
      showRoot();
      return;
    }
    const saved = await putTournamentSettings({ tournaments_root: p });
    if (saved) {
      root = rootFrom(saved);
      window.dispatchEvent(new CustomEvent(APP_EVT.TOURNAMENTS_ROOT_CHANGED));
    }
    showRoot();
  };
  const rootRow = pathRow(
    "Tournaments root",
    root.path,
    "directory",
    "Pick tournaments root",
    changeRoot,
  );
  showRoot();
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
