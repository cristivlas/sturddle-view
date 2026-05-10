import { showDialog } from "./dialogs.js";

export async function openAboutDialog({ api }) {
  let version = "", copyright = "";
  try {
    const s = await api("GET", "/settings");
    version = s.version || "";
    copyright = s.copyright || "";
  } catch {
    // show whatever we have
  }

  await showDialog({
    label: "About SturddleView",
    width: "min(300px, 90vw)",
    body: (_resolve, dialog) => {
      const wrap = document.createElement("div");
      wrap.className = "about-dialog";

      const name = document.createElement("div");
      name.className = "about-name";
      name.textContent = "SturddleView";

      const ver = document.createElement("div");
      ver.className = "about-meta";
      ver.textContent = `Version ${version}`;

      const copy = document.createElement("div");
      copy.className = "about-meta";
      copy.textContent = `(c) ${copyright}`;

      wrap.append(name, ver, copy);
      dialog.append(wrap);
    },
  });
}
