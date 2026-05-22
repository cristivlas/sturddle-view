// Arena perspective: engine-vs-engine tournament configuration / running.
// Engine roster management lives in Settings -> Engines.

import { mountTournaments } from "../tournaments.js";
import { getActiveWorkspace } from "../tournament-workspace.js";

export const enginesPerspective = {
  id: "engines",
  label: "Arena",

  async mount(root, ctx) {
    root.innerHTML = `<section id="engines-perspective"></section>`;

    const host = root.querySelector("#engines-perspective");
    const tournamentsCtl = mountTournaments({
      container: host,
      api: ctx.api,
      events: ctx.events,
      log: ctx.log,
      token: ctx.token,
    });

    // Tournament workspace stays visible while this perspective is mounted.
    getActiveWorkspace()?.show();
    tournamentsCtl?.restoreWorkspace?.();

    // Announce the active ribbon for the global float manager.
    const ribbonEl = host.querySelector(".tournaments-ribbon");
    window.dispatchEvent(new CustomEvent("sturddle:ribbon-active", { detail: { el: ribbonEl } }));

    return {
      unmount() {
        tournamentsCtl?.dismissSortToast?.();
        getActiveWorkspace()?.close();
        tournamentsCtl?.unmount?.();
        window.dispatchEvent(new CustomEvent("sturddle:ribbon-active", { detail: { el: null } }));
      },
    };
  },
};
