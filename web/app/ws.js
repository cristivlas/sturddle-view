// Minimal WebSocket client with exponential-backoff reconnect.

// First epoch observed this page-load. If the server restarts, the next
// event carries a different epoch and we reload to drop stale UI state
// (view-mode cursor, dismissed toasts, edit drafts) that the new server
// session can't honor.
let _sessionEpoch = null;

export function connect({ onOpen, onClose, onEvent }) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  // Auth carried by HttpOnly cookie set during /auth handshake.
  const url = `${proto}//${location.host}/ws`;

  let backoff = 500;
  const maxBackoff = 15000;
  let ws;
  let stopped = false;
  let reconnectTimer = null;

  function open() {
    ws = new WebSocket(url);
    ws.addEventListener("open", () => {
      backoff = 500;
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
      backoff = Math.min(maxBackoff, Math.round(backoff * 1.7));
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
