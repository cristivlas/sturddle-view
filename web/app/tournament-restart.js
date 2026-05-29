// Shared "Restart from scratch" confirm dialog payload + wipe-confirm
// query string. Used by both the workspace's failure-banner Restart
// button and the tournaments list's start verb -- two callers, one
// truth for the user-facing copy and the server-handshake flag.

export const CONFIRM_WIPE_QS = "confirm_wipe=true";

export function buildRestartConfirm(name, games) {
  const message = games > 0
    ? `Restart "${name}" from scratch? All ${games} recorded games will be permanently deleted.`
    : `Restart "${name}" from scratch?`;
  return { message, okLabel: "Restart", destructive: true };
}
