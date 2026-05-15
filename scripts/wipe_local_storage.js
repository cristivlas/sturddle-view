// Paste into DevTools console at the SturddleView app URL.
// Removes all sturddle*/sturddle-view* localStorage keys AND clears the
// server-side recent-imports store so the app repopulates from scratch.
(async () => {
  const keys = Object.keys(localStorage).filter(
    k => k.startsWith("sturddle") || k.startsWith("sturddle-view") || k.startsWith("fs-picker:")
  );
  console.log("Removing", keys.length, "localStorage keys:", keys);
  keys.forEach(k => localStorage.removeItem(k));

  // Server-side: list every recent-import and DELETE it. Auth is via
  // the session cookie set by /auth, so credentials: "include"
  // is all we need.
  try {
    const list = await fetch("/game/recent-imports", { credentials: "include" }).then(r => r.json());
    const entries = list.entries || [];
    console.log("Removing", entries.length, "server recent-imports");
    for (const e of entries) {
      await fetch(`/game/recent-imports/${e.hash}`, { method: "DELETE", credentials: "include" });
    }
  } catch (e) {
    console.warn("server-side wipe failed:", e);
  }
  console.log("Done. Hard-reload (Ctrl+Shift+R) to repopulate.");
})();
