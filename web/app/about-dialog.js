import { showDialog } from "./dialogs.js";
import { KIND } from "./game-events.js";

const CONNECT_SUMMARY = "Connect from mobile";
const HINT_OPENING = "Opening the LAN port...";
const HINT_LAN_OFF = "LAN access is off; restart with --host 0.0.0.0";
const HINT_OFFLINE = "No network connection";
const HINT_BIND_FAILED = "Could not open the LAN port (see log)";
// Mirrors REASON_* in server/sturddle_view/api/connect.py.
const REASON_LOOPBACK_BIND = "loopback_bind";
const REASON_BIND_FAILED = "bind_failed";
const HINT_BY_REASON = {
  [REASON_LOOPBACK_BIND]: HINT_LAN_OFF,
  [REASON_BIND_FAILED]: HINT_BIND_FAILED,
};
const WA_AFTER_SHOW = "wa-after-show";

async function fetchOrNull(api, method, path) {
  try {
    return await api(method, path);
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

function renderConnectBody(info) {
  if (info?.qr) {
    const img = document.createElement("img");
    img.className = "about-qr";
    img.src = info.qr;
    img.alt = CONNECT_SUMMARY;
    return img;
  }
  return textDiv("about-meta", HINT_BY_REASON[info?.reason] ?? HINT_OFFLINE);
}

// Loopback clients only (a phone that already reached us has no use for
// its own QR). Opening the LAN port -- and with it the Windows Firewall
// prompt, which can hold the request for a while -- waits for the first
// expand, so it happens when the user asks.
function buildConnectSection(api) {
  const details = document.createElement("wa-details");
  details.summary = CONNECT_SUMMARY;
  details.iconPlacement = "start";
  details.className = "about-connect";
  details.append(textDiv("about-meta", HINT_OPENING));
  details.addEventListener(WA_AFTER_SHOW, async () => {
    details.replaceChildren(renderConnectBody(await fetchOrNull(api, "POST", "/connect/lan")));
  }, { once: true });
  return details;
}

export async function openAboutDialog({ api, events }) {
  const [settings, connect] = await Promise.all([
    fetchOrNull(api, "GET", "/settings"),
    fetchOrNull(api, "GET", "/connect"),
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
      if (connect?.local) wrap.append(buildConnectSection(api));
      dialog.append(wrap);
    },
  });
  unsubscribe();
}
