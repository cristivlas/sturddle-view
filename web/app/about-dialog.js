import { showDialog } from "./dialogs.js";
import { KIND } from "./game-events.js";

const CONNECT_SUMMARY = "Connect from mobile";
const HINT_LAN_OFF = "LAN access is off; restart with --host 0.0.0.0";
const HINT_OFFLINE = "No network connection";

async function fetchOrNull(api, path) {
  try {
    return await api("GET", path);
  } catch {
    return null;
  }
}

function textDiv(className, text) {
  const div = document.createElement("div");
  div.className = className;
  div.textContent = text;
  return div;
}

// Loopback clients only (the server omits the QR for everyone else):
// a phone that already reached us has no use for its own QR code.
function buildConnectSection(info) {
  if (!info?.local) return null;
  const details = document.createElement("wa-details");
  details.summary = CONNECT_SUMMARY;
  details.iconPlacement = "start";
  details.className = "about-connect";
  if (info.qr) {
    const img = document.createElement("img");
    img.className = "about-qr";
    img.src = info.qr;
    img.alt = CONNECT_SUMMARY;
    details.append(img);
  } else {
    details.append(textDiv("about-meta", info.lan_enabled ? HINT_OFFLINE : HINT_LAN_OFF));
  }
  return details;
}

export async function openAboutDialog({ api, events }) {
  const [settings, connect] = await Promise.all([
    fetchOrNull(api, "/settings"),
    fetchOrNull(api, "/connect"),
  ]);
  const version = settings?.version || "";
  const copyright = settings?.copyright || "";

  let unsubscribe = () => {};
  await showDialog({
    width: "min(300px, 90vw)",
    body: (resolve, dialog) => {
      // The phone just scanned the QR: the dialog has done its job.
      unsubscribe = events.on((evt) => {
        if (evt.kind === KIND.REMOTE_CONNECTED) resolve();
      });
      const wrap = document.createElement("div");
      wrap.className = "about-dialog";
      wrap.append(
        textDiv("about-name", "SturddleView"),
        textDiv("about-meta", `Version ${version}`),
        textDiv("about-meta", `(c) ${copyright}`),
      );
      const connectSection = buildConnectSection(connect);
      if (connectSection) wrap.append(connectSection);
      dialog.append(wrap);
    },
  });
  unsubscribe();
}
