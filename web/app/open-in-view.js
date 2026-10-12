// Open a game in Play's view mode from outside play.js: the tournament
// Review button and the Settings Recents tab. Play/view state comes from
// GET /game/status, not a client mirror: mirrors are unseeded on a fresh
// page load and would skip the confirmations.

import { confirm } from "./dialogs.js";
import { confirmReplaceViewedGame } from "./import-position-dialog.js";
import { APP_EVT } from "./app-events.js";

export const GAME_STATUS_PATH = "/game/status";
const IMPORT_PATH = "/game/import";
const JSON_HEADERS = { "Content-Type": "application/json" };
const PLAY_PERSPECTIVE = "play";
const CANCEL_LABEL = "Cancel";

export const OPEN_VERDICT = {
  CANCEL: "cancel",
  // The game is already in view: skip the import, just show Play.
  ALREADY_VIEWING: "already_viewing",
  PROCEED: "proceed",
};

// Ask what leaving the current game needs: `leaveMessage` when a game is in
// progress, the replace-viewed confirm when another game is in view.
export async function confirmOpenInView(status, { hash, summary, leaveMessage, okLabel }) {
  if (status.in_progress) {
    const ok = await confirm({ message: leaveMessage, okLabel, cancelLabel: CANCEL_LABEL });
    return ok ? OPEN_VERDICT.PROCEED : OPEN_VERDICT.CANCEL;
  }
  if (!status.viewing) return OPEN_VERDICT.PROCEED;
  if (hash && hash === status.view_hash) return OPEN_VERDICT.ALREADY_VIEWING;
  const ok = await confirmReplaceViewedGame({
    currentHash: status.view_hash,
    currentSummary: status.view_summary,
    incomingHash: hash,
    incomingSummary: summary,
    analysisRunning: status.analyzing,
  });
  return ok ? OPEN_VERDICT.PROCEED : OPEN_VERDICT.CANCEL;
}

// POST the import; resolves to the server's game_id. Throws on failure.
export async function importForView({ text, format, gameId = null }) {
  const body = { text, format };
  if (gameId) body.game_id = gameId;
  const res = await fetch(IMPORT_PATH, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`import -> ${res.status}`);
  const data = await res.json();
  return data?.game_id ?? null;
}

// Show Play. After an import, pass `remount`: a Play already on screen
// drops board updates for a game id it didn't switch to itself, so it must
// remount and resync from the server.
export function activatePlay({ remount = false } = {}) {
  window.dispatchEvent(new CustomEvent(APP_EVT.ACTIVATE_PERSPECTIVE, {
    detail: { id: PLAY_PERSPECTIVE, remount },
  }));
}
