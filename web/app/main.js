import { connect } from "./ws.js";
import { mountBoard } from "./board.js";

const params = new URLSearchParams(location.search);
const token = params.get("token") || "";

const conn = document.getElementById("conn-status");
const engineInfo = document.getElementById("engine-info");
const eventLog = document.getElementById("event-log");
const agentPanel = document.getElementById("agent-panel");

function append(el, text, max = 200) {
  el.textContent = (el.textContent + text + "\n").split("\n").slice(-max).join("\n");
}

async function api(method, path, body) {
  const r = await fetch(`${path}?token=${encodeURIComponent(token)}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`${method} ${path} -> ${r.status} ${detail}`);
  }
  return r.json();
}

const board = mountBoard({
  element: document.getElementById("board"),
  onMove: async (uci) => {
    try {
      await api("POST", "/game/move", { uci });
    } catch (e) {
      append(eventLog, `move rejected: ${e.message}`);
    }
  },
});

let activeGameId = null;

function handleEvent(evt) {
  switch (evt.kind) {
    case "engine_info":
      append(engineInfo, JSON.stringify(evt.payload));
      break;
    case "agent_annotation": {
      const div = document.createElement("div");
      div.textContent = evt.payload.text || JSON.stringify(evt.payload);
      agentPanel.prepend(div);
      break;
    }
    case "board_update":
      board.setPosition(evt.payload.fen, evt.payload.last_move);
      // Enable input only when it's the human's turn. The server's `human_white`
      // matches the side selected at /game/new; we just compare against board orientation.
      board.enableInput(true);
      break;
    case "game_result":
      append(eventLog, `result: ${JSON.stringify(evt.payload)}`);
      board.enableInput(false);
      break;
    default:
      append(eventLog, `${evt.kind}: ${JSON.stringify(evt.payload)}`);
  }
}

document.getElementById("new-game").addEventListener("click", async () => {
  const side = document.getElementById("human-side").value;
  const initial = parseFloat(document.getElementById("initial-seconds").value);
  const inc = parseFloat(document.getElementById("increment-seconds").value);
  board.setSide(side);
  try {
    const r = await api("POST", "/game/new", {
      human_white: side === "white",
      initial_seconds: initial,
      increment_seconds: inc,
    });
    activeGameId = r.game_id;
    append(eventLog, `new game: ${activeGameId}`);
  } catch (e) {
    append(eventLog, `new-game failed: ${e.message}`);
  }
});

document.getElementById("resign").addEventListener("click", async () => {
  try {
    await api("POST", "/game/resign", {});
  } catch (e) {
    append(eventLog, `resign failed: ${e.message}`);
  }
});

connect({
  token,
  onOpen: () => {
    conn.textContent = "connected";
    conn.classList.add("connected");
  },
  onClose: () => {
    conn.textContent = "reconnecting…";
    conn.classList.remove("connected");
  },
  onEvent: handleEvent,
});
