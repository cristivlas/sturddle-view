// Dockable AI analysis prose window. Mirrors the UCI Log / Search Lines
// windows so it shares the same managed dock (with N-slot resize) in
// the play perspective.
//
// Opens on first chunk, appends every `ai_info` delta, finishes when
// payload carries done=true (and shows a "cancelled" marker if done &&
// cancelled). Lifecycle (open/close on Analyze click, restore on
// perspective remount) is driven by play.js -- this module only owns
// the dom inside the window.

import { createDockableWindow, DOCK_ORDER } from "./play-dock-windows.js";

const GEO_KEY       = "sturddle:ai:geo";
const WIN_STATE_KEY = "sturddle:ai:winstate";
const DOCKED_KEY    = "sturddle:ai:docked";
const OPEN_KEY      = "sturddle:ai:open";

// Status text shown next to a spinner while a turn is in flight. The
// LLM may take seconds (model latency + engine tool calls) before any
// prose lands; without this, an empty panel reads as "stuck". Keep
// the wording short -- the panel is narrow.
const STATUS_TEXT = {
  idle: "",
  waiting: "Analyzing...",
  engine: "Running engine search...",
};

function buildBody() {
  const root = document.createElement("div");
  root.className = "play-ai-body";
  root.tabIndex = 0;

  // Status line: spinner + text, hidden until a turn starts. Lives
  // above the prose so the prose can stream in below without jumping.
  const status = document.createElement("div");
  status.className = "play-ai-status";
  status.hidden = true;
  const spinner = document.createElement("wa-spinner");
  spinner.size = "medium";
  const statusText = document.createElement("span");
  statusText.className = "play-ai-status-text";
  status.append(spinner, statusText);

  const para = document.createElement("p");
  para.className = "play-ai-prose";
  root.append(status, para);
  root._status = status;
  root._statusText = statusText;
  root._para = para;
  root._hasContent = false;
  return root;
}

const inst = createDockableWindow({
  title: "AI Analysis",
  className: "sturddle-wb-ai",
  geoKey: GEO_KEY,
  winStateKey: WIN_STATE_KEY,
  dockedKey: DOCKED_KEY,
  openKey: OPEN_KEY,
  defaultW: () => 380,
  defaultH: 300,
  defaultY: () => 100,
  build() {
    return buildBody();
  },
  dockOrder: DOCK_ORDER.AI_ANALYSIS,
  closable: true,
});

export function openAi() {
  if (inst.wb || inst.slot) return;
  inst.toggle(null);
}

export function closeAi() {
  inst.close();
}

export function isAiOpen() {
  return !!(inst.wb || inst.slot);
}

export function resetAi() {
  if (!inst.body) return;
  inst.body._para.textContent = "";
  inst.body._hasContent = false;
  setAiStatus("waiting");
}

export function setAiStatus(state) {
  // `state` in: idle | waiting | engine.
  // Spinner shows on waiting/engine; hidden on idle. First text delta
  // implicitly clears the status (see appendAiDelta).
  if (!inst.body) return;
  const text = STATUS_TEXT[state] ?? "";
  if (!text) {
    inst.body._status.hidden = true;
    inst.body._statusText.textContent = "";
    return;
  }
  inst.body._status.hidden = false;
  inst.body._statusText.textContent = text;
}

export function appendAiDelta(text) {
  if (!inst.body || !text) return;
  if (!inst.body._hasContent) {
    inst.body._para.textContent = "";
    inst.body._hasContent = true;
    // Prose has started flowing -- hide the spinner.
    setAiStatus("idle");
  }
  inst.body._para.append(document.createTextNode(text));
}

export function markAiDone({
  cancelled = false,
  error = null,
  errorDetail = null,
  roundCap = false,
} = {}) {
  // Terminal: clear spinner, then render whichever marker applies
  // (error > roundCap > cancelled if multiple flags are set).
  setAiStatus("idle");
  if (!inst.body) return;
  if (error) {
    const block = document.createElement("div");
    block.className = "play-ai-error";
    const head = document.createElement("strong");
    head.textContent = "AI analysis failed";
    block.append(head);
    if (errorDetail) {
      const body = document.createElement("div");
      body.className = "play-ai-error-detail";
      body.textContent = errorDetail;
      block.append(body);
    }
    inst.body._para.append(block);
    return;
  }
  if (roundCap) {
    const note = document.createElement("div");
    note.className = "play-ai-roundcap";
    note.textContent = "Stopped early at the tool-call cap. Raise SV_AI_MAX_TOOL_ROUNDS to allow more rounds.";
    inst.body._para.append(note);
    return;
  }
  if (cancelled) {
    const marker = document.createElement("span");
    marker.className = "play-ai-cancelled";
    marker.textContent = " [cancelled]";
    inst.body._para.append(marker);
  }
}
