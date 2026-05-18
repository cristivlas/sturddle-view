// Arena perspective: engine-vs-engine tournament configuration / running.
// Engine roster management lives in Settings -> Engines.

import { mountTournaments } from "../tournaments.js";
import { getActiveWorkspace } from "../tournament-workspace.js";

export const enginesPerspective = {
  id: "engines",
  label: "Arena",

  async mount(root, ctx) {
    root.innerHTML = `
      <section id="engines-perspective">
        <div class="tournaments-host"></div>
      </section>
    `;

    const tournamentsHost = root.querySelector(".tournaments-host");
    const tournamentsCtl = mountTournaments({
      container: tournamentsHost,
      api: ctx.api,
      events: ctx.events,
      log: ctx.log,
      token: ctx.token,
    });

    // Tournament workspace stays visible while this perspective is mounted.
    getActiveWorkspace()?.show();
    tournamentsCtl?.restoreWorkspace?.();

    return {
      unmount() {
        tournamentsCtl?.dismissSortToast?.();
        getActiveWorkspace()?.close();
        tournamentsCtl?.unmount?.();
      },
    };
  },
};
