// Engines perspective: subsumes engine roster management and tournament
// configuration / running. The previous "Observe" sub-tab is gone — its
// function (live tournament view) is now an "Open workspace" verb on a
// tournament row inside the Tournaments sub-tab.

import { mountEngines } from "../engines.js";
import { mountTournaments } from "../tournaments.js";
import { getActiveWorkspace } from "../tournament-workspace.js";

const SUBTAB_STORAGE_KEY = "sturddle-view:engines-subtab";

export const enginesPerspective = {
  id: "engines",
  label: "Engines",

  async mount(root, ctx) {
    root.innerHTML = `
      <section id="engines-perspective">
        <wa-tab-group placement="top" class="engines-subnav">
          <wa-tab slot="nav" panel="roster">Roster</wa-tab>
          <wa-tab slot="nav" panel="tournaments">Tournaments</wa-tab>

          <wa-tab-panel name="roster">
            <div class="engines-roster-host"></div>
          </wa-tab-panel>
          <wa-tab-panel name="tournaments">
            <div class="tournaments-host"></div>
          </wa-tab-panel>
        </wa-tab-group>
      </section>
    `;

    const rosterHost = root.querySelector(".engines-roster-host");
    mountEngines({
      container: rosterHost,
      api: ctx.api,
    });

    const tournamentsHost = root.querySelector(".tournaments-host");
    const tournamentsCtl = mountTournaments({
      container: tournamentsHost,
      api: ctx.api,
      events: ctx.events,
      log: ctx.log,
      token: ctx.token,
    });

    // Tab-driven workspace visibility: Tournament boards stay hidden
    // unless the Tournaments sub-tab is active.
    const tabGroup = root.querySelector(".engines-subnav");
    const applyVisibility = (panelName) => {
      if (panelName === "tournaments") getActiveWorkspace()?.show();
      else { tournamentsCtl?.dismissSortToast?.(); getActiveWorkspace()?.hide(); }
    };
    const onTabShow = (e) => {
      const name = e.detail?.name;
      try { localStorage.setItem(SUBTAB_STORAGE_KEY, name); } catch { /* */ }
      applyVisibility(name);
    };
    tabGroup.addEventListener("wa-tab-show", onTabShow);
    // Restore last-used sub-tab and apply visibility. wa-tab-show may
    // not fire for the default tab, so we set it manually after the
    // custom element is defined.
    customElements.whenDefined("wa-tab-group").then(() => {
      let saved = null;
      try { saved = localStorage.getItem(SUBTAB_STORAGE_KEY); } catch { /* */ }
      if (saved && saved !== tabGroup.active) tabGroup.active = saved;
      applyVisibility(tabGroup.active || tabGroup.activeTab?.panel);
    });

    return {
      unmount() {
        tabGroup.removeEventListener("wa-tab-show", onTabShow);
        tournamentsCtl?.unmount?.();
      },
    };
  },
};
