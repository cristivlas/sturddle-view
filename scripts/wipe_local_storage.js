// Paste into DevTools console at the SturddleView app URL.
// Removes all sturddle*/sturddle-view* localStorage keys so the app
// repopulates from scratch.
(() => {
  const keys = Object.keys(localStorage).filter(
    k => k.startsWith("sturddle") || k.startsWith("sturddle-view") || k.startsWith("fs-picker:")
  );
  console.log("Removing", keys.length, "keys:", keys);
  keys.forEach(k => localStorage.removeItem(k));
})();
