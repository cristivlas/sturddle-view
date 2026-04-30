// Engines perspective: subsumes engine management, tournament config, and
// live tournament observation. Inner nav splits into Roster, Tournaments,
// and Observe sub-views.

import { mountEngines } from "../engines.js";

export const enginesPerspective = {
  id: "engines",
  label: "Engines",

  async mount(root, ctx) {
    root.innerHTML = `
      <section id="engines-perspective">
        <wa-tab-group placement="top" class="engines-subnav">
          <wa-tab slot="nav" panel="roster">Roster</wa-tab>
          <wa-tab slot="nav" panel="tournaments">Tournaments</wa-tab>
          <wa-tab slot="nav" panel="observe">Observe</wa-tab>

          <wa-tab-panel name="roster">
            <div class="engines-roster-host"></div>
          </wa-tab-panel>
          <wa-tab-panel name="tournaments">
            <div class="placeholder">
              <p>Tournaments — coming soon.</p>
              <p class="muted">Configure pairings, time controls, SPRT parameters; start/stop runs.</p>
            </div>
          </wa-tab-panel>
          <wa-tab-panel name="observe">
            <div class="placeholder">
              <p>Observe — coming soon.</p>
              <p class="muted">Workspace canvas for live and headless tournament viewing.</p>
            </div>
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

    return { unmount() {} };
  },
};
