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
import {
  AUTOSCROLL_SLACK_PROSE_PX,
  isPinnedToBottom,
  scrollToBottom,
} from "./wb-utils.js";

const GEO_KEY       = "sturddle:ai:geo";
const WIN_STATE_KEY = "sturddle:ai:winstate";
const DOCKED_KEY    = "sturddle:ai:docked";
const OPEN_KEY      = "sturddle:ai:open";
// Last model name shown in the panel title. Pinned at analyze-start;
// reload restores so the title reflects what last ran, not what is
// currently selected in Settings.
const TITLE_MODEL_KEY = "sturddle:ai:title-model";

// Status text shown next to a spinner while a turn is in flight. The
// LLM may take seconds (model latency + engine tool calls) before any
// prose lands; without this, an empty panel reads as "stuck". Keep
// the wording short -- the panel is narrow.
const STATUS_TEXT = {
  idle: "",
  waiting: "Analyzing...",
  engine: "Running engine search...",
  done: "Analysis Done",
};

// Sticky open/closed pref for the Thinking disclosure block.
const THINKING_OPEN_KEY = "sturddle:ai:thinking-open";

function readThinkingOpen() {
  try { return localStorage.getItem(THINKING_OPEN_KEY) === "1"; } catch { return false; }
}

function writeThinkingOpen(open) {
  try { localStorage.setItem(THINKING_OPEN_KEY, open ? "1" : "0"); } catch { /* */ }
}

function buildBody() {
  const root = document.createElement("div");
  root.className = "play-ai-body";
  root.tabIndex = 0;

  // Status line: spinner + text, hidden until a turn starts. Lives
  // above the rounds so they can stream in below without jumping.
  const status = document.createElement("div");
  status.className = "play-ai-status";
  status.hidden = true;
  const spinner = document.createElement("wa-spinner");
  spinner.size = "medium";
  const statusText = document.createElement("span");
  statusText.className = "play-ai-status-text";
  status.append(spinner, statusText);

  // Scroll wrapper. Holds rounds + terminal; the status header above
  // it stays put because only this wrapper scrolls.
  const scroll = document.createElement("div");
  scroll.className = "play-ai-scroll";

  const rounds = document.createElement("div");
  rounds.className = "play-ai-rounds";

  const terminal = document.createElement("div");
  terminal.className = "play-ai-terminal";

  scroll.append(rounds, terminal);
  root.append(status, scroll);
  root._status = status;
  root._statusText = statusText;
  root._scroll = scroll;
  root._rounds = rounds;
  root._terminal = terminal;
  // Map roundIndex -> {panel, thinking:{details,body}, tools, para,
  //   hasProse, revision}. Built lazily on first event per round.
  root._roundPanels = new Map();
  root._currentRound = null;
  // Pending revision banner data keyed by the round it applies to.
  // The ai_corrective event arrives before that round's first chunk.
  root._pendingRevision = new Map();
  // tool_use_id -> tool-call line DOM node, so a failure event can
  // mark the exact row by id (not by tool name or position).
  root._toolCallNodes = new Map();
  return root;
}

function buildRoundPanel() {
  const panel = document.createElement("section");
  panel.className = "play-ai-round";
  // Optional revision banner (only on rounds triggered by a
  // validator hit on the previous round). A <details> so clicking
  // the banner reveals the previous round's redacted prose inline.
  const revision = document.createElement("details");
  revision.className = "play-ai-revision";
  revision.hidden = true;
  const revisionSummary = document.createElement("summary");
  revisionSummary.className = "play-ai-revision-summary";
  const revisionBody = document.createElement("div");
  revisionBody.className = "play-ai-revision-body";
  revision.append(revisionSummary, revisionBody);
  // Timeline: Thinking + tool calls share one container so a single
  // CSS left rule connects them visually. Prose is outside the
  // timeline so it isn't crossed by the rule.
  const timeline = document.createElement("div");
  timeline.className = "play-ai-timeline";
  const details = document.createElement("details");
  details.className = "play-ai-thinking";
  const summary = document.createElement("summary");
  summary.textContent = "Thinking";
  const thinkBody = document.createElement("div");
  thinkBody.className = "play-ai-thinking-body";
  details.append(summary, thinkBody);
  details.hidden = true;
  const tools = document.createElement("div");
  tools.className = "play-ai-tools";
  timeline.append(details, tools);
  const para = document.createElement("p");
  para.className = "play-ai-prose";
  panel.append(revision, timeline, para);
  return {
    panel,
    revision: { details: revision, summary: revisionSummary, body: revisionBody },
    thinking: { details, body: thinkBody },
    tools, para,
    hasProse: false,
  };
}

function ensureRoundPanel(root, roundIndex) {
  let entry = root._roundPanels.get(roundIndex);
  if (entry) return entry;
  // Collapse the previously-current round's thinking; the new round
  // takes the sticky-pref slot.
  if (root._currentRound !== null) {
    const prev = root._roundPanels.get(root._currentRound);
    if (prev) prev.thinking.details.open = false;
  }
  entry = buildRoundPanel();
  // New round is the "current" one -- its thinking honors the sticky
  // pref. (Older rounds always default closed.)
  entry.thinking.details.open = readThinkingOpen();
  entry.thinking.details.addEventListener("toggle", () => {
    // Only persist the pref when toggling the round that's still
    // current; old-round toggles are explicit user inspection and
    // shouldn't change the default for new turns.
    if (root._currentRound === roundIndex) {
      writeThinkingOpen(entry.thinking.details.open);
    }
  });
  // Pending revision banner for this round?
  const rev = root._pendingRevision.get(roundIndex);
  if (rev) {
    renderRevision(entry.revision, rev);
    root._pendingRevision.delete(roundIndex);
    if (roundIndex > 0) {
      const prev = root._roundPanels.get(roundIndex - 1);
      if (prev) _moveProseIntoRevision(prev, entry);
    }
  }
  root._rounds.append(entry.panel);
  root._roundPanels.set(roundIndex, entry);
  root._currentRound = roundIndex;
  return entry;
}

function renderRevision({ details, summary }, { illegalMoves, falseClaims, castleViolations }) {
  details.hidden = false;
  summary.textContent = "";  // reset
  const head = document.createElement("strong");
  head.textContent = "Revision: ";
  summary.append(head);
  const parts = [];
  if (illegalMoves && illegalMoves.length) {
    parts.push(`not valid: ${illegalMoves.join(", ")}`);
  }
  if (falseClaims && falseClaims.length) {
    parts.push(falseClaims.map((c) => `no ${c}`).join(", "));
  }
  if (castleViolations && castleViolations.length) {
    parts.push("castling not legal");
  }
  summary.append(document.createTextNode(parts.join("; ")));
}

function formatToolArgs(input) {
  // Compact one-line summary of the args. The full payload lives in
  // the transcript; the panel just needs a glanceable label.
  if (!input || typeof input !== "object") return "";
  const pairs = Object.entries(input).map(([k, v]) => {
    const s = typeof v === "string" ? v : JSON.stringify(v);
    return `${k}=${s}`;
  });
  return pairs.join(", ");
}

// Move the previous round's prose into the current round's revision
// banner body so clicking the banner reveals the redacted text inline.
// Idempotent -- skips when the prose has already been moved out of
// the prior round's panel.
function _moveProseIntoRevision(prevEntry, currentEntry) {
  const para = prevEntry.para;
  if (!para || !para.isConnected) return;
  if (para.parentNode === currentEntry.revision.body) return;
  currentEntry.revision.body.append(para);
}


let userCloseHandler = null;
let reanalyzeHandler = null;

// Title-bar custom actions. className must be unique so the factory's
// post-mount lookup in floating mode does not collide with body content.
const TITLE_ACTIONS = [
  {
    className: "play-ai-reanalyze-ctrl",
    title: "Re-analyze",
    onClick: () => { if (reanalyzeHandler) reanalyzeHandler(); },
  },
];

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
  onUserClose: () => {
    if (userCloseHandler) userCloseHandler();
  },
  titleActions: TITLE_ACTIONS,
});

export function setOnUserCloseAi(fn) {
  userCloseHandler = fn;
}

export function setOnReanalyzeAi(fn) {
  reanalyzeHandler = fn;
}

export function setAiTitle(modelName) {
  const name = modelName || "";
  const t = name ? `AI Analysis (${name})` : "AI Analysis";
  inst.setTitle(t);
  try {
    if (name) localStorage.setItem(TITLE_MODEL_KEY, name);
    else localStorage.removeItem(TITLE_MODEL_KEY);
  } catch { /* quota / disabled storage; non-fatal */ }
}

// Restore the last-run title on module load so a page reload does not
// reset the panel to the bare "AI Analysis" label.
try {
  const saved = localStorage.getItem(TITLE_MODEL_KEY);
  if (saved) inst.setTitle(`AI Analysis (${saved})`);
} catch { /* non-fatal */ }

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

// The actual scroller is the inner .play-ai-scroll wrapper; the
// host bodies (wb.body / .dock-slot-body) are overflow:hidden so the
// status header above the wrapper stays put.
function withStickyBottom(fn) {
  if (!inst.body) return;
  const scroller = inst.body._scroll;
  const pinned = isPinnedToBottom(scroller, AUTOSCROLL_SLACK_PROSE_PX);
  fn();
  if (pinned) scrollToBottom(scroller);
}

export function resetAi() {
  if (!inst.body) return;
  inst.body._rounds.textContent = "";
  inst.body._terminal.textContent = "";
  inst.body._roundPanels.clear();
  inst.body._pendingRevision.clear();
  inst.body._toolCallNodes.clear();
  inst.body._currentRound = null;
  setAiStatus("waiting");
}

export function appendAiThinking(text, roundIndex = 0) {
  if (!inst.body || !text) return;
  withStickyBottom(() => {
    const entry = ensureRoundPanel(inst.body, roundIndex);
    entry.thinking.details.hidden = false;
    entry.thinking.body.append(document.createTextNode(text));
  });
}

export function appendAiToolCall({ round = 0, name, input, toolUseId }) {
  if (!inst.body || !name) return;
  withStickyBottom(() => {
    const entry = ensureRoundPanel(inst.body, round);
    const line = document.createElement("div");
    line.className = "play-ai-tool-call";
    const dot = document.createElement("span");
    dot.className = `play-ai-tool-dot play-ai-tool-dot-${name}`;
    line.append(dot);
    const label = document.createElement("span");
    label.className = "play-ai-tool-label";
    const args = formatToolArgs(input);
    label.textContent = args ? `${name}(${args})` : `${name}()`;
    line.append(label);
    entry.tools.append(line);
    if (toolUseId) {
      // Index by tool_use_id so a subsequent ai_tool_call_failed event
      // can mark this exact row (multiple calls of the same tool in a
      // round would otherwise collide).
      inst.body._toolCallNodes.set(toolUseId, line);
    }
  });
}

export function appendAiToolCallComplete({ round = 0, name }) {
  if (!inst.body) return;
  withStickyBottom(() => {
    const entry = ensureRoundPanel(inst.body, round);
    const line = document.createElement("div");
    line.className = "play-ai-tool-call";
    const dot = document.createElement("span");
    dot.className = "play-ai-tool-dot play-ai-tool-dot-complete";
    line.append(dot);
    const label = document.createElement("span");
    label.className = "play-ai-tool-label";
    label.textContent = name ? `${name} complete` : "tool complete";
    line.append(label);
    entry.tools.append(line);
  });
}

export function markAiToolCallFailed({ toolUseId, error, detail }) {
  if (!inst.body || !toolUseId) return;
  const line = inst.body._toolCallNodes.get(toolUseId);
  if (!line) return;
  line.classList.add("play-ai-tool-call-failed");
  const label = line.querySelector(".play-ai-tool-label");
  if (label) {
    const suffix = detail ? `${error}: ${detail}` : error;
    label.textContent = `${label.textContent}  — ${suffix}`;
  }
}

export function noteAiRevision({ round, illegalMoves, falseClaims, castleViolations }) {
  if (!inst.body) return;
  // Mark the previous round as invalidated so its prose styles as
  // overruled. The prose itself is moved into the new round's revision
  // banner body once that panel exists -- ensureRoundPanel handles the
  // move when ordering goes pending -> create -> render.
  if (round > 0) {
    const prev = inst.body._roundPanels.get(round - 1);
    if (prev) prev.panel.classList.add("play-ai-round-invalidated");
  }
  // The ai_corrective event arrives before the round's first chunk.
  // Stash so ensureRoundPanel renders the banner when the panel is
  // created. If the panel already exists (rare; chunk ordering
  // surprise), render and move immediately.
  const payload = { illegalMoves, falseClaims, castleViolations };
  const existing = inst.body._roundPanels.get(round);
  if (existing) {
    renderRevision(existing.revision, payload);
    if (round > 0) {
      const prev = inst.body._roundPanels.get(round - 1);
      if (prev) _moveProseIntoRevision(prev, existing);
    }
  } else {
    inst.body._pendingRevision.set(round, payload);
  }
}

export function setAiStatus(state) {
  // `state` in: idle | waiting | engine | done.
  // Spinner shows on waiting/engine; hidden on idle/done. First text
  // delta no longer hides the status (the header stays as a label).
  if (!inst.body) return;
  const text = STATUS_TEXT[state] ?? "";
  if (state === "idle" || !text) {
    inst.body._status.hidden = true;
    inst.body._statusText.textContent = "";
    return;
  }
  inst.body._status.hidden = false;
  inst.body._statusText.textContent = text;
  const spinner = inst.body._status.querySelector("wa-spinner");
  if (spinner) spinner.style.display = (state === "done") ? "none" : "";
}

export function appendAiDelta(text, roundIndex = 0) {
  if (!inst.body || !text) return;
  withStickyBottom(() => {
    const entry = ensureRoundPanel(inst.body, roundIndex);
    if (!entry.hasProse) entry.hasProse = true;
    entry.para.append(document.createTextNode(text));
  });
}

export function markAiDone({
  cancelled = false,
  error = null,
  errorDetail = null,
  roundCap = false,
  noResponse = false,
} = {}) {
  // Terminal: switch the header text + drop the spinner. Markers
  // (error > roundCap > noResponse > cancelled if any apply) land in
  // the dedicated terminal slot below the last round panel.
  const naturalCompletion = !error && !roundCap && !noResponse && !cancelled;
  setAiStatus(naturalCompletion ? "done" : "idle");
  if (!inst.body) return;
  const slot = inst.body._terminal;
  withStickyBottom(() => {
    // Natural completion on a multi-round turn gets a subtle divider
    // below the final prose, so the user has a clear "AI is done"
    // signal. Skipped on cancel/error/round-cap/no-response (their
    // own markers fill that role) and on single-round turns (nothing
    // to separate from).
    const multiRound = inst.body._roundPanels.size > 1;
    if (multiRound && naturalCompletion) {
      const last = inst.body._roundPanels.get(inst.body._currentRound);
      if (last) last.para.classList.add("play-ai-prose-final");
    }
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
      slot.append(block);
      return;
    }
    if (roundCap) {
      const note = document.createElement("div");
      note.className = "play-ai-roundcap";
      note.textContent = "Stopped early at the tool-call cap. Raise SV_AI_MAX_TOOL_ROUNDS to allow more rounds.";
      slot.append(note);
      return;
    }
    if (noResponse) {
      const note = document.createElement("div");
      note.className = "play-ai-roundcap";
      note.textContent = "Model produced no answer. Try a different model -- some stream only chain-of-thought.";
      slot.append(note);
      return;
    }
    if (cancelled) {
      const marker = document.createElement("span");
      marker.className = "play-ai-cancelled";
      marker.textContent = "[cancelled]";
      slot.append(marker);
    }
  });
}
