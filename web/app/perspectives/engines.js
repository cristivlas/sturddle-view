// Engines perspective: subsumes engine roster management and tournament
// configuration / running. The previous "Observe" sub-tab is gone — its
// function (live tournament view) is now an "Open workspace" verb on a
// tournament row inside the Tournaments sub-tab.

import { mountEngines } from "../engines.js";
import { mountTournaments } from "../tournaments.js";

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
      onError: (msg) => ctx.log(msg),
    });

    const tournamentsHost = root.querySelector(".tournaments-host");
    const tournamentsCtl = mountTournaments({
      container: tournamentsHost,
      api: ctx.api,
      events: ctx.events,
      log: ctx.log,
    });

    return {
      unmount() {
        tournamentsCtl?.unmount?.();
      },
    };
  },
};
