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

// Label shown on the Thinking disclosure summary while a round's
// thinking stream is still arriving. Swapped to "Thought for Ns" once
// any non-thinking chunk lands (prose delta or tool call), since that's
// the signal the model finished thinking for this round.
const THINKING_LABEL_ACTIVE = "Thinking";
const THINKING_LABEL_DONE_PREFIX = "Thought for ";

// Self-correct effect: how long the overruled prose lingers struck-out
// in place before it collapses into the revision banner.
const REVISION_STRIKE_MS = 2000;
const PROSE_OVERRULING_CLASS = "play-ai-prose-overruling";

function readThinkingOpen() {
  try { return localStorage.getItem(THINKING_OPEN_KEY) === "1"; } catch { return false; }
}

function writeThinkingOpen(open) {
  try { localStorage.setItem(THINKING_OPEN_KEY, open ? "1" : "0"); } catch { /* */ }
}

function trimTrailingWhitespace(el) {
  // Strip trailing whitespace from the element's last text node, then
  // drop trailing text nodes that became empty. Lets CSS :empty kick
  // in for ws-only content.
  if (!el) return;
  while (el.lastChild && el.lastChild.nodeType === Node.TEXT_NODE) {
    const trimmed = el.lastChild.nodeValue.replace(/\s+$/, "");
    if (trimmed) {
      el.lastChild.nodeValue = trimmed;
      return;
    }
    el.removeChild(el.lastChild);
  }
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
  summary.textContent = THINKING_LABEL_ACTIVE;
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
    thinking: { details, summary, body: thinkBody },
    tools, para,
    hasProse: false,
    hasThinking: false,
    thinkingStartedAt: 0,
    thinkingDurationMs: 0,
  };
}

function freezeThinkingLabel(entry) {
  // Snap the disclosure summary from "Thinking" to "Thought for Ns" the
  // first time any non-thinking chunk arrives for this round. Idempotent
  // so prose + tool calls landing in either order both work.
  if (!entry || !entry.hasThinking || entry.thinkingDurationMs > 0) return;
  const elapsedMs = entry.thinkingStartedAt
    ? Date.now() - entry.thinkingStartedAt
    : 0;
  entry.thinkingDurationMs = Math.max(1, elapsedMs);
  entry.thinking.summary.textContent =
    `${THINKING_LABEL_DONE_PREFIX}${formatThinkingDuration(entry.thinkingDurationMs)}`;
  entry.thinking.summary.classList.remove("is-active");
}

function formatThinkingDuration(ms) {
  // Under a minute: "Ns" (minimum 1s so sub-second flashes don't read
  // as "0s"). At/over a minute: "m:ss". Hours are unrealistic for a
  // single turn so we don't format past minutes.
  const totalSeconds = Math.max(1, Math.round(ms / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function ensureRoundPanel(root, roundIndex) {
  let entry = root._roundPanels.get(roundIndex);
  if (entry) return entry;
  // Collapse the previously-current round's thinking; the new round
  // takes the sticky-pref slot. Also freeze its summary label: the
  // model has demonstrably moved on, so "Thinking ..." would just
  // sit there spinning forever on rounds that had no prose/tool
  // chunk to trigger the freeze.
  if (root._currentRound !== null) {
    const prev = root._roundPanels.get(root._currentRound);
    if (prev) {
      prev.thinking.details.open = false;
      freezeThinkingLabel(prev);
    }
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

const TOOL_FRIENDLY_LABELS = {
  analyze:        "Analyzing position",
  top_moves:      "Finding top moves",
  piece_at:       "Checking piece",
  validate_move:  "Validating move",
  recommend_move: "Picking move",
  material:       "Counting material",
  delegate:       "Verifying line",
};

function friendlyToolLabel(name) {
  return TOOL_FRIENDLY_LABELS[name] || name;
}

// Tuck a round's overruled prose into its own revision banner body so
// clicking the banner reveals the redacted text inline. Idempotent --
// skips when the prose is already inside the banner.
function _moveProseIntoRevision(entry) {
  const para = entry.para;
  if (!para || !para.isConnected) return;
  if (para.parentNode === entry.revision.body) return;
  entry.revision.body.append(para);
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
  inst.body._toolCallNodes.clear();
  inst.body._currentRound = null;
  setAiStatus("waiting");
}

export function appendAiThinking(text, roundIndex = 0) {
  if (!inst.body || !text) return;
  withStickyBottom(() => {
    const entry = ensureRoundPanel(inst.body, roundIndex);
    // Drop leading whitespace until the first non-ws char arrives, so a
    // model that opens with " \n" doesn't show an empty Thinking pane.
    const out = entry.hasThinking ? text : text.replace(/^\s+/, "");
    if (!out) return;
    if (!entry.hasThinking) {
      entry.thinkingStartedAt = Date.now();
      entry.thinking.summary.classList.add("is-active");
    }
    entry.hasThinking = true;
    entry.thinking.details.hidden = false;
    entry.thinking.body.append(document.createTextNode(out));
  });
}

export function appendAiToolCall({
  round = 0, name, input, toolUseId, parentToolUseId = null,
}) {
  if (!inst.body || !name) return;
  withStickyBottom(() => {
    const line = document.createElement("div");
    line.className = "play-ai-tool-call";
    // Verifier-origin calls nest under their "Verifying line" (delegate)
    // row so the user sees them as that check's work, not the narrator's.
    // Fall back to the round panel if the parent row isn't found.
    let container = null;
    if (parentToolUseId) {
      const parent = inst.body._toolCallNodes.get(parentToolUseId);
      if (parent) {
        let children = parent.querySelector(".play-ai-tool-children");
        if (!children) {
          children = document.createElement("div");
          children.className = "play-ai-tool-children";
          parent.append(children);
        }
        container = children;
      }
    }
    if (!container) {
      const entry = ensureRoundPanel(inst.body, round);
      freezeThinkingLabel(entry);
      container = entry.tools;
    }
    const dot = document.createElement("span");
    dot.className = `play-ai-tool-dot play-ai-tool-dot-${name}`;
    line.append(dot);
    const label = document.createElement("span");
    label.className = "play-ai-tool-label";
    label.textContent = friendlyToolLabel(name);
    line.append(label);
    const args = formatToolArgs(input);
    const raw = args ? `${name}(${args})` : `${name}()`;
    const toggle = document.createElement("span");
    toggle.className = "play-ai-tool-toggle";
    // One glyph, rotated via CSS when open -- guarantees the open/closed
    // caret are identical size (the unicode triangles aren't).
    toggle.textContent = "▶";
    line.append(toggle);
    const pre = document.createElement("pre");
    pre.className = "play-ai-tool-details-body";
    pre.hidden = true;
    pre.textContent = raw;
    line.append(pre);
    toggle.addEventListener("click", () => {
      const open = pre.hidden;
      pre.hidden = !open;
      toggle.classList.toggle("is-open", open);
    });
    container.append(line);
    if (toolUseId) {
      // Index by tool_use_id so a subsequent ai_tool_call_failed event
      // can mark this exact row (multiple calls of the same tool in a
      // round would otherwise collide).
      inst.body._toolCallNodes.set(toolUseId, line);
    }
  });
}


export function markAiToolCallFailed({ toolUseId, error, detail }) {
  if (!inst.body || !toolUseId) return;
  const line = inst.body._toolCallNodes.get(toolUseId);
  if (!line) return;
  line.classList.add("play-ai-tool-call-failed");
  const suffix = detail ? `${error}: ${detail}` : error;
  const pre = line.querySelector(".play-ai-tool-details-body");
  if (pre) pre.textContent = `${pre.textContent}\n${suffix}`;
}

export function noteAiRevision({ round, illegalMoves, falseClaims, castleViolations }) {
  if (!inst.body) return;
  // The correction at `round` overrules `round - 1`. Host the revision
  // banner on the overruled round's OWN panel and tuck its prose into
  // that banner -- so it always shows, even when `round` never renders
  // a panel of its own (turn ends/caps right after the correction).
  if (round <= 0) return;
  const prev = inst.body._roundPanels.get(round - 1);
  if (!prev) return;
  const payload = { illegalMoves, falseClaims, castleViolations };
  // Self-correct effect: strike the prose in place for a beat, THEN
  // reveal the banner and collapse the prose into it -- so the user
  // sees the model scratch its claim before it's tucked away. The
  // timer is this effect's own cadence (not a sync wait).
  const para = prev.para;
  if (para && para.isConnected && para.parentNode !== prev.revision.body) {
    para.classList.add(PROSE_OVERRULING_CLASS);
    setTimeout(() => {
      para.classList.remove(PROSE_OVERRULING_CLASS);
      renderRevision(prev.revision, payload);
      _moveProseIntoRevision(prev);
    }, REVISION_STRIKE_MS);
  } else {
    renderRevision(prev.revision, payload);
    _moveProseIntoRevision(prev);
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
    // Drop leading whitespace until the first non-ws char arrives;
    // prevents an empty-looking bordered box on rounds whose prose
    // starts with stray newlines from the model.
    const out = entry.hasProse ? text : text.replace(/^\s+/, "");
    if (!out) return;
    if (!entry.hasProse) freezeThinkingLabel(entry);
    entry.hasProse = true;
    entry.para.append(document.createTextNode(out));
  });
}

export function markAiDone({
  cancelled = false,
  error = null,
  errorDetail = null,
  roundCap = false,
  noResponse = false,
  noRecommendation = false,
} = {}) {
  // Terminal: switch the header text + drop the spinner. Markers (error >
  // roundCap > noResponse > noRecommendation > cancelled if any apply)
  // land in the dedicated terminal slot below the last round panel.
  const naturalCompletion =
    !error && !roundCap && !noResponse && !noRecommendation && !cancelled;
  setAiStatus(naturalCompletion ? "done" : "idle");
  if (!inst.body) return;
  const slot = inst.body._terminal;
  withStickyBottom(() => {
    // Trim trailing whitespace on every round's prose and thinking so a
    // model that closes with a newline doesn't leave an empty-looking
    // last line under the final border.
    for (const entry of inst.body._roundPanels.values()) {
      trimTrailingWhitespace(entry.para);
      trimTrailingWhitespace(entry.thinking.body);
      freezeThinkingLabel(entry);
    }
    // Natural completion on a multi-round turn gets a subtle divider
    // below the final prose, so the user has a clear "AI is done"
    // signal. Skipped on cancel/error/round-cap/no-response (their
    // own markers fill that role) and on single-round turns (nothing
    // to separate from).
    const multiRound = inst.body._roundPanels.size > 1;
    if (multiRound && naturalCompletion) {
      const last = inst.body._roundPanels.get(inst.body._currentRound);
      // Skip when the last round's prose was overruled and tucked into
      // its (collapsed) Revision banner -- the border would land on
      // text the user can't see.
      const overruled = last && last.para.parentNode === last.revision.body;
      if (last && !overruled) last.para.classList.add("play-ai-prose-final");
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
    if (noRecommendation) {
      const note = document.createElement("div");
      note.className = "play-ai-roundcap";
      note.textContent = "No move chosen -- the analysis finished without committing to one.";
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
