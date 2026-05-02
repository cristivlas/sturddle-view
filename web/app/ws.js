// Minimal WebSocket client with exponential-backoff reconnect.

export function connect({ token, onOpen, onClose, onEvent }) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws?token=${encodeURIComponent(token)}`;

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
        onEvent?.(JSON.parse(e.data));
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
