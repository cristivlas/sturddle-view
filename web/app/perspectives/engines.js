// Arena/Studio perspective: engine-vs-engine tournament configuration /
// running. The TOURNAMENT_UX setting picks which UX mounts; engine roster
// management lives in Settings -> Engines.

import { mountTournaments } from "../tournaments.js";
import { APP_EVT } from "../app-events.js";
import { getActiveWorkspace } from "../tournament-workspace.js";
import { getTournamentUx, mountTournamentStudio, TOURNAMENT_UX } from "../tournament-studio.js";

function mountArena(host, ctx) {
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
  window.dispatchEvent(new CustomEvent(APP_EVT.RIBBON_ACTIVE, { detail: { el: ribbonEl } }));

  return {
    unmount() {
      tournamentsCtl?.dismissSortToast?.();
      getActiveWorkspace()?.close();
      tournamentsCtl?.unmount?.();
      window.dispatchEvent(new CustomEvent(APP_EVT.RIBBON_ACTIVE, { detail: { el: null } }));
    },
  };
}

export const enginesPerspective = {
  id: "engines",
  label: "Arena",

  async mount(root, ctx) {
    root.innerHTML = `<section id="engines-perspective"></section>`;
    const host = root.querySelector("#engines-perspective");
    return getTournamentUx() === TOURNAMENT_UX.STUDIO
      ? mountTournamentStudio({ container: host, api: ctx.api, events: ctx.events, log: ctx.log, token: ctx.token })
      : mountArena(host, ctx);
  },
};
