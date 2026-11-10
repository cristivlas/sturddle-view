// This client's token scope (server auth.py). "host" -- the desktop window
// or console link -- can do anything; "remote" -- the Connect-from-mobile
// QR -- can play and watch, but not open Settings or set up engines and
// tournaments. The server enforces it; the UI only hides what would fail.

const ROLE_PATH = "/auth/role";
const ROLE_REMOTE = "remote";
// Body class that hides every `.host-only` control (styles.css).
const REMOTE_CLIENT_CLASS = "remote-client";
export const HOST_ONLY_CLASS = "host-only";

let remote = false;

/** Fetch the role once at startup, before any UI is built. On failure,
 *  assume host: nothing gets hidden and the server still refuses. */
export async function loadRole() {
  try {
    const r = await fetch(ROLE_PATH);
    if (r.ok) remote = (await r.json()).role === ROLE_REMOTE;
  } catch { /* keep host */ }
  document.body.classList.toggle(REMOTE_CLIENT_CLASS, remote);
}

export function isRemote() {
  return remote;
}
