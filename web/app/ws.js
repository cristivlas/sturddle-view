// Minimal WebSocket client with exponential-backoff reconnect.

const BACKOFF_INITIAL_MS = 500;
const BACKOFF_MAX_MS = 15000;
const BACKOFF_FACTOR = 1.7;

// First epoch observed this page-load. If the server restarts, the next
// event carries a different epoch and we reload to drop stale UI state
// (view-mode cursor, dismissed toasts, edit drafts) that the new server
// session can't honor.
let _sessionEpoch = null;

// `path` is resolved against the page's host. Auth carried by the HttpOnly
// cookie set during the /auth handshake.
export function connect({ path, onOpen, onClose, onEvent }) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${path}`;

  let backoff = BACKOFF_INITIAL_MS;
  let ws;
  let stopped = false;
  let reconnectTimer = null;

  function open() {
    ws = new WebSocket(url);
    ws.addEventListener("open", () => {
      backoff = BACKOFF_INITIAL_MS;
      onOpen?.();
    });
    ws.addEventListener("message", (e) => {
      try {
        const evt = JSON.parse(e.data);
        const epoch = evt.session_epoch;
        if (epoch) {
          if (_sessionEpoch === null) {
            _sessionEpoch = epoch;
          } else if (_sessionEpoch !== epoch) {
            // Server restarted; reload to resync.
            location.reload();
            return;
          }
        }
        onEvent?.(evt);
      } catch (err) {
        console.error("bad event payload", err, e.data);
      }
    });
    ws.addEventListener("close", () => {
      onClose?.();
      if (stopped) return;
      reconnectTimer = setTimeout(() => { reconnectTimer = null; open(); }, backoff);
      backoff = Math.min(BACKOFF_MAX_MS, Math.round(backoff * BACKOFF_FACTOR));
    });
    ws.addEventListener("error", () => ws.close());
  }

  open();
  return {
    close() {
      stopped = true;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      ws?.close();
    },
  };
}
