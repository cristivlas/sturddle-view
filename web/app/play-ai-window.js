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
  markSelectable,
  scrollToBottom,
} from "./wb-utils.js";
import { STORAGE_KEY } from "./storage-keys.js";
import { loadRaw, saveRaw } from "./storage.js";
import {
  openSettings,
  SETTINGS_TAB_ANALYSIS,
  errorActionsFor,
  buildToastActionButton,
} from "./dialogs.js";

const GEO_KEY       = STORAGE_KEY.AI_GEO;
const WIN_STATE_KEY = STORAGE_KEY.AI_WIN_STATE;
const DOCKED_KEY    = STORAGE_KEY.AI_DOCKED;
const OPEN_KEY      = STORAGE_KEY.AI_OPEN;
// Last model name shown in the panel title. Pinned at analyze-start;
// reload restores so the title reflects what last ran, not what is
// currently selected in Settings.
const TITLE_MODEL_KEY = STORAGE_KEY.AI_TITLE_MODEL;

export const AI_STATE = Object.freeze({
  IDLE: "idle", WAITING: "waiting", ENGINE: "engine", DONE: "done",
});

// Status text shown next to a spinner while a turn is in flight. The
// LLM may take seconds (model latency + engine tool calls) before any
// prose lands; without this, an empty panel reads as "stuck". Keep
// the wording short -- the panel is narrow.
const STATUS_TEXT = {
  [AI_STATE.IDLE]: "",
  [AI_STATE.WAITING]: "Analyzing...",
  [AI_STATE.ENGINE]: "Running engine search...",
  [AI_STATE.DONE]: "Analysis Done",
};

// Sticky open/closed pref for the Thinking disclosure block.
const THINKING_OPEN_KEY = STORAGE_KEY.AI_THINKING_OPEN;
const THINKING_OPEN_ON = "1";
const THINKING_OPEN_OFF = "0";

// Compact token count for the status ticker (Claude Code style):
// 982, 14.2k, 1.3M.
const TOKENS_PER_K = 1000;
const TOKENS_PER_M = 1000000;

function fmtTokensCompact(n) {
  if (n >= TOKENS_PER_M) return `${(n / TOKENS_PER_M).toFixed(1)}M`;
  if (n >= TOKENS_PER_K) return `${(n / TOKENS_PER_K).toFixed(1)}k`;
  return String(n);
}

// Per-provider billing ratios relative to the input-token price, for
// the effective-input figure -- ratios, not dollar prices, so they
// don't drift per model. Anthropic: cache reads 0.1x, writes (5-minute
// TTL) 1.25x. Gemini: implicit-cache reads ~0.25x, writes free.
// Providers not listed (or unknown) show raw counts without eff-in.
const EFF_IN_WEIGHTS = {
  anthropic: { read: 0.1, write: 1.25 },
  gemini: { read: 0.25, write: 0 },
};

// Terminal breakdown line, e.g. "tokens: 12,340 in (11,020 cached) - 1,846
// out - ~2.6k eff. in". "in" is the full prompt volume (uncached + cache
// reads + cache writes); "eff. in" is that volume weighted by the
// provider's billing ratios -- what the input side actually cost in
// input-priced tokens. Cache + eff-in figures appear only when caching
// was active AND the provider's ratios are known.
function fmtUsageSummary(u, provider) {
  const cached = u.cache_read_input_tokens || 0;
  const written = u.cache_creation_input_tokens || 0;
  const fresh = u.input_tokens || 0;
  const inTotal = fresh + cached + written;
  const out = u.output_tokens || 0;
  const sep = " \u00b7 ";
  const inPart = cached > 0
    ? `${inTotal.toLocaleString()} in (${cached.toLocaleString()} cached)`
    : `${inTotal.toLocaleString()} in`;
  let line = `tokens: ${inPart}${sep}${out.toLocaleString()} out`;
  const weights = EFF_IN_WEIGHTS[provider];
  if (weights && (cached > 0 || written > 0)) {
    const effective = Math.round(
      fresh + written * weights.write + cached * weights.read
    );
    line += `${sep}~${fmtTokensCompact(effective)} eff. in`;
  }
  return line;
}

function usageTotal(u) {
  return (u.input_tokens || 0) + (u.output_tokens || 0)
    + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
}

// Label shown on the Thinking disclosure summary while a round's
// thinking stream is still arriving. Swapped to "Thought for Ns" once
// any non-thinking chunk lands (prose delta or tool call), since that's
// the signal the model finished thinking for this round.
const THINKING_LABEL_ACTIVE = "Thinking";
const THINKING_LABEL_DONE_PREFIX = "Thought for ";

const MS_PER_SECOND = 1000;
const SECONDS_PER_MINUTE = 60;
const SECONDS_PAD = 2;

// Panel DOM classes shared between builders and selectors.
const IS_ACTIVE_CLASS = "is-active";
const ERROR_CLASS = "play-ai-error";
const PROSE_CLASS = "play-ai-prose";
const REVISION_CLASS = "play-ai-revision";
const REVISION_SUMMARY_CLASS = "play-ai-revision-summary";
const REVISION_BODY_CLASS = "play-ai-revision-body";
const TOOL_CALL_CLASS = "play-ai-tool-call";
const TOOL_HEAD_CLASS = "play-ai-tool-head";
const TOOL_DOT_CLASS = "play-ai-tool-dot";
const TOOL_CHILDREN_CLASS = "play-ai-tool-children";
const TOOL_DETAILS_BODY_CLASS = "play-ai-tool-details-body";
const ROUNDCAP_CLASS = "play-ai-roundcap";

// Server-side agent tool names.
const TOOL = Object.freeze({
  ANALYZE: "analyze",
  TOP_MOVES: "top_moves",
  PIECE_AT: "piece_at",
  VALIDATE_MOVE: "validate_move",
  RECOMMEND_MOVE: "recommend_move",
  MATERIAL: "material",
  TACTICS: "tactics",
  DELEGATE: "delegate",
  REPORT_LINE: "report_line",
  RELATED_OPENINGS: "related_openings",
  POSITION_JUDGE: "position_judge",
  // Progress steps of recommend_move's check, nested under its row: which
  // search the board arrow and Search Lines are showing right now.
  ENGINE_BEST: "engine_best",
  SEARCH_MOVE: "search_move",
});

// Per-tool dot class (tool identity color; also scopes verdict/cleared badges).
function toolDotClass(name) {
  return `${TOOL_DOT_CLASS}-${name}`;
}

function toolCallStateClass(state) {
  return `${TOOL_CALL_CLASS}-${state}`;
}

// The row's own dot, not a nested child row's.
function hasOwnDot(line, dotClass) {
  return !!line.querySelector(`:scope > .${TOOL_HEAD_CLASS} > .${dotClass}`);
}

function readThinkingOpen() {
  return loadRaw(THINKING_OPEN_KEY) === THINKING_OPEN_ON;
}

function writeThinkingOpen(open) {
  saveRaw(THINKING_OPEN_KEY, open ? THINKING_OPEN_ON : THINKING_OPEN_OFF);
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

  // Status line: spinner + text, hidden until a turn starts. Lives
  // above the rounds so they can stream in below without jumping.
  const status = document.createElement("div");
  status.className = "play-ai-status";
  status.hidden = true;
  const spinner = document.createElement("wa-spinner");
  spinner.className = "spinner-accent";
  root._spinner = spinner;
  const statusText = document.createElement("span");
  statusText.className = "play-ai-status-text";
  // Token ticker: cumulative turn usage, updated per provider round
  // (Claude Code style). Empty until the provider reports usage.
  const statusTokens = document.createElement("span");
  statusTokens.className = "play-ai-status-tokens";
  status.append(spinner, statusText, statusTokens);

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

  root._hoveredTarget = null;
  scroll.addEventListener("mouseover", ev => {
    root._hoveredTarget = ev.target;
  });
  scroll.addEventListener("mouseleave", () => {
    root._hoveredTarget = null;
  });

  // Select the hovered block (tool detail / error / prose), falling
  // back to the current round's prose paragraph.
  markSelectable(root, { target: () => {
    const hovered = root._hoveredTarget;
    const detailPre = hovered?.closest(`.${TOOL_DETAILS_BODY_CLASS}`);
    const errorBlock = hovered?.closest(`.${ERROR_CLASS}`);
    const prosePara = hovered?.closest(`.${PROSE_CLASS}`);
    return (detailPre && !detailPre.hidden)
      ? detailPre
      : errorBlock
      ?? prosePara
      ?? root._roundPanels.get(root._currentRound)?.para;
  } });

  root._status = status;
  root._statusText = statusText;
  root._statusTokens = statusTokens;
  root._scroll = scroll;
  root._rounds = rounds;
  root._terminal = terminal;
  // Map roundIndex -> {panel, thinking:{details,body}, tools, para,
  //   revision, hasProse}. Built lazily on first event per round.
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
  para.className = PROSE_CLASS;
  // Revision: a collapsible that folds away prose the AI corrected. The
  // summary carries a self-correction line; the body holds the struck-through
  // flawed prose.
  const revision = document.createElement("details");
  revision.className = REVISION_CLASS;
  revision.hidden = true;
  const revisionSummary = document.createElement("summary");
  revisionSummary.className = REVISION_SUMMARY_CLASS;
  const revisionBody = document.createElement("div");
  revisionBody.className = REVISION_BODY_CLASS;
  revision.append(revisionSummary, revisionBody);
  panel.append(timeline, para, revision);
  return {
    panel,
    thinking: { details, summary, body: thinkBody },
    tools, para,
    revision: { details: revision, summary: revisionSummary, body: revisionBody },
    hasProse: false,
    hasThinking: false,
    thinkingStartedAt: 0,
    thinkingDurationMs: 0,
  };
}

function freezeThinkingLabel(entry, serverMs = null) {
  // Snap "Thinking" -> "Thought for Ns" on the first non-thinking chunk.
  // Idempotent. Prefer serverMs (rides the event, correct on replay);
  // fall back to the local Date.now() delta when absent.
  if (!entry || !entry.hasThinking || entry.thinkingDurationMs > 0) return;
  const elapsedMs = Number.isFinite(serverMs)
    ? serverMs
    : entry.thinkingStartedAt
    ? Date.now() - entry.thinkingStartedAt
    : 0;
  entry.thinkingDurationMs = Math.max(1, elapsedMs);
  entry.thinking.summary.textContent =
    `${THINKING_LABEL_DONE_PREFIX}${formatThinkingDuration(entry.thinkingDurationMs)}`;
  entry.thinking.summary.classList.remove(IS_ACTIVE_CLASS);
}

function formatThinkingDuration(ms) {
  // Under a minute: "Ns" (minimum 1s so sub-second flashes don't read
  // as "0s"). At/over a minute: "m:ss". Hours are unrealistic for a
  // single turn so we don't format past minutes.
  const totalSeconds = Math.max(1, Math.round(ms / MS_PER_SECOND));
  if (totalSeconds < SECONDS_PER_MINUTE) return `${totalSeconds}s`;
  const m = Math.floor(totalSeconds / SECONDS_PER_MINUTE);
  const s = totalSeconds % SECONDS_PER_MINUTE;
  return `${m}:${String(s).padStart(SECONDS_PAD, "0")}`;
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

// Each tool maps to a few interchangeable phrasings so the panel doesn't
// repeat the same label on every call; pickVariant() rotates through them.
const TOOL_FRIENDLY_LABELS = {
  [TOOL.ANALYZE]:        ["Analyzing position", "Studying the position", "Weighing the position", "Assessing", "Grasping the situation", "Navel-gazing", "Dubito ergo cogito"],
  [TOOL.TOP_MOVES]:      [
    "Finding copacetic moves", "Triangulating", "Scoping out top moves", "Brainstorming",
    "Smelling own ideas", "Scheming", "Conjuring power moves", "Crop-circling",
    "Deploying analytical probes",
  ],
  [TOOL.PIECE_AT]:       ["Checking", "Eyeing", "Zooming in on", "Targeting"],
  [TOOL.VALIDATE_MOVE]:  ["Validating move", "Double-checking the move", "Confirming the move"],
  [TOOL.RECOMMEND_MOVE]: ["Picking move", "Choosing a move", "Selecting a move", "Sussing out"],
  [TOOL.MATERIAL]:       ["Counting material", "Tallying material", "Weighing material"],
  [TOOL.TACTICS]:        ["Spotting pins and forks", "Eyeing pins and forks", "Sniffing out tactics"],
  [TOOL.DELEGATE]:       ["Verifying line", "Double-checking the play", "Reviewing the plan", "Simulating", "Fathoming", "Choreographing"],
  [TOOL.REPORT_LINE]:    ["Checking line", "Reviewing the line", "Going over the idea", "Ascertaining"],
  [TOOL.RELATED_OPENINGS]: ["Comparing openings", "Cross-checking openings", "Matching openings"],
  [TOOL.POSITION_JUDGE]: ["Checking position claims", "Fact-checking the position", "Verifying the claims"],
};

// Tools whose label shows the actual move under consideration ("Considering
// Nd3"). Maps the tool to its verb variants; the move SAN from input.move is
// appended.
const MOVE_TOOL_VERBS = {
  [TOOL.RECOMMEND_MOVE]: ["Considering", "Weighing", "Contemplating", "Ratiocinating over", "Projecting"],
  [TOOL.VALIDATE_MOVE]:  ["Validating", "Probing", "Confirming", "Vetting"],
  [TOOL.DELEGATE]:       ["Verifying", "Double-checking", "Reviewing", "War-gaming"],
};

// The dominance check's two steps share one metaphor: the baseline search
// (the engine's own best) sets the standard, the candidate is measured
// against it. Each pair is [baseline, candidate]; "{}" is the move slot. One
// pick per check, keyed on the parent row, so the two rows always match.
const CHECK_STEP_PAIRS = [
  ["Setting the bar",        "Seeing if {} clears the bar"],
  ["Consulting the oracle",  "Putting {} to the oracle"],
  ["Finding the main line",  "Testing {} against the main line"],
];
const CHECK_STEP_SLOT = "{}";
const CHECK_STEP_INDEX = { [TOOL.ENGINE_BEST]: 0, [TOOL.SEARCH_MOVE]: 1 };
// parent row (the recommend_move line) -> its pair; the baseline step picks,
// the candidate step reuses. Cleared with the tool-call node index.
const checkStepPairs = new Map();

function checkStepLabel(name, input, parent) {
  const slot = CHECK_STEP_INDEX[name];
  if (slot === undefined) return null;
  let pair = parent ? checkStepPairs.get(parent) : null;
  if (!pair) {
    pair = pickVariant(CHECK_STEP_PAIRS);
    if (parent) checkStepPairs.set(parent, pair);
  }
  const move = input && typeof input.move === "string" ? input.move.trim() : "";
  return pair[slot].replace(CHECK_STEP_SLOT, move);
}

// Last variant index handed out per variants array (keyed by array identity,
// so TOOL_FRIENDLY_LABELS and MOVE_TOOL_VERBS entries for the same tool name
// cycle independently). Cleared in resetAi() so a new analysis run doesn't
// carry bias from the previous one.
const toolLabelLastIndex = new Map();

// Random pick that excludes the immediately-previous variant, so the same
// label never shows twice in a row without falling into a predictable cycle.
function pickVariant(variants) {
  if (variants.length === 1) return variants[0];
  const last = toolLabelLastIndex.get(variants);
  let idx;
  do {
    idx = Math.floor(Math.random() * variants.length);
  } while (idx === last);
  toolLabelLastIndex.set(variants, idx);
  return variants[idx];
}

const ANALYSIS_WINDOW_TITLE = "Analysis";

// Default floating geometry (px).
const DEFAULT_WIN_W = 380;
const DEFAULT_WIN_H = 300;
const DEFAULT_WIN_Y = 100;

// Expanded tool-call detail rows: the call under IN, the result under OUT.
const IO_TAG_IN = "IN";
const IO_TAG_OUT = "OUT";

function buildIoRow(tag, text) {
  const row = document.createElement("div");
  row.className = "play-ai-tool-io";
  const tagEl = document.createElement("span");
  tagEl.className = "play-ai-tool-io-tag";
  tagEl.textContent = tag;
  const pre = document.createElement("pre");
  pre.className = "play-ai-tool-io-text";
  pre.textContent = text;
  row.append(tagEl, pre);
  return { row, pre };
}

function formatToolOutput(output) {
  return typeof output === "string" ? output : JSON.stringify(output);
}

function friendlyToolLabel(name, input, parent = null) {
  const step = checkStepLabel(name, input, parent);
  if (step !== null) return step;
  const move = input && typeof input.move === "string" ? input.move.trim() : "";
  const verbs = MOVE_TOOL_VERBS[name];
  if (verbs && move) return `${pickVariant(verbs)} ${move}`;
  const labels = TOOL_FRIENDLY_LABELS[name];
  if (name === TOOL.PIECE_AT) {
    const square = input && typeof input.square === "string" ? input.square.trim() : "";
    if (square) return `${pickVariant(labels)} ${square}`;
  }
  return labels ? pickVariant(labels) : name;
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

// Mobile inline host (set by play.js on mount). On phones the dock column is
// hidden and floating chrome is unusable, so the panel mounts here, stacked
// below the board. See createDockableWindow's inline placement.
let inlineEl = null;

const inst = createDockableWindow({
  title: ANALYSIS_WINDOW_TITLE,
  className: "sturddle-wb-ai",
  geoKey: GEO_KEY,
  winStateKey: WIN_STATE_KEY,
  dockedKey: DOCKED_KEY,
  openKey: OPEN_KEY,
  defaultW: () => DEFAULT_WIN_W,
  defaultH: DEFAULT_WIN_H,
  defaultY: () => DEFAULT_WIN_Y,
  build() {
    return buildBody();
  },
  dockOrder: DOCK_ORDER.AI_ANALYSIS,
  railDockable: true,
  getInlineEl: () => inlineEl,
  closable: true,
  onUserClose: () => {
    if (userCloseHandler) userCloseHandler();
  },
  titleActions: TITLE_ACTIONS,
});

export function setAiInlineHost(el) {
  inlineEl = el || null;
}

export function setOnUserCloseAi(fn) {
  userCloseHandler = fn;
}

export function setOnReanalyzeAi(fn) {
  reanalyzeHandler = fn;
}

export function setAiTitle(modelName) {
  const name = modelName || "";
  const t = name ? `${ANALYSIS_WINDOW_TITLE} (${name})` : ANALYSIS_WINDOW_TITLE;
  inst.setTitle(t);
  saveRaw(TITLE_MODEL_KEY, name || null);
}

// Restore the last-run title on module load so a page reload does not
// reset the panel to the bare "Analysis" label.
const saved = loadRaw(TITLE_MODEL_KEY);
if (saved) inst.setTitle(`${ANALYSIS_WINDOW_TITLE} (${saved})`);

export function openAi() {
  if (inst.mounted) return;
  inst.toggle(null);
}

export function closeAi() {
  inst.close();
}

export function isAiOpen() {
  return inst.mounted;
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
  inst.body._statusTokens.textContent = "";
  inst.body._roundPanels.clear();
  inst.body._toolCallNodes.clear();
  checkStepPairs.clear();
  inst.body._currentRound = null;
  toolLabelLastIndex.clear();
  setAiStatus(AI_STATE.WAITING);
}

// Cumulative turn usage from ai_usage events. Idempotent (overwrites
// with the latest totals), which is exactly what replay re-dispatch
// needs. The ticker counts output tokens only -- Claude Code semantics
// (tokens the model generated, not the re-sent prefix); the full
// input/cache accounting lands in the terminal slot on done.
export function setAiUsage(usage) {
  if (!inst.body || !usage) return;
  const out = usage.output_tokens || 0;
  inst.body._statusTokens.textContent =
    out > 0 ? `${fmtTokensCompact(out)} tokens` : "";
}

// Delta-less thinking event carrying only a server duration, for a round
// whose thinking no prose/tool event surfaced (see _ThinkTimer.flush_ms).
export function freezeAiThinking(roundIndex = 0, thinkingMs = null) {
  if (!inst.body) return;
  const entry = inst.body._roundPanels.get(roundIndex);
  if (entry) freezeThinkingLabel(entry, thinkingMs);
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
      entry.thinking.summary.classList.add(IS_ACTIVE_CLASS);
    }
    entry.hasThinking = true;
    entry.thinking.details.hidden = false;
    entry.thinking.body.append(document.createTextNode(out));
  });
}

export function appendAiToolCall({
  round = 0, name, input, toolUseId, parentToolUseId = null, thinkingMs = null,
}) {
  if (!inst.body || !name) return;
  withStickyBottom(() => {
    const line = document.createElement("div");
    line.className = TOOL_CALL_CLASS;
    // Verifier-origin calls nest under their "Verifying line" (delegate)
    // row so the user sees them as that check's work, not the narrator's.
    // Fall back to the round panel if the parent row isn't found.
    let container = null;
    let parent = null;
    if (parentToolUseId) {
      parent = inst.body._toolCallNodes.get(parentToolUseId) ?? null;
      if (parent) {
        let children = parent.querySelector(`.${TOOL_CHILDREN_CLASS}`);
        if (!children) {
          children = document.createElement("div");
          children.className = TOOL_CHILDREN_CLASS;
          parent.append(children);
        }
        container = children;
      }
    }
    if (!container) {
      const entry = ensureRoundPanel(inst.body, round);
      freezeThinkingLabel(entry, thinkingMs);
      container = entry.tools;
    }
    // Dot/label/arrow live in a nowrap head that scrolls horizontally,
    // so a long label never wraps the arrow onto its own line.
    const head = document.createElement("div");
    head.className = TOOL_HEAD_CLASS;
    line.append(head);
    const dot = document.createElement("span");
    dot.className = `${TOOL_DOT_CLASS} ${toolDotClass(name)}`;
    head.append(dot);
    const label = document.createElement("span");
    label.className = "play-ai-tool-label";
    label.textContent = friendlyToolLabel(name, input, parent);
    head.append(label);
    const args = formatToolArgs(input);
    const raw = args ? `${name}(${args})` : `${name}()`;
    const toggle = document.createElement("span");
    toggle.className = "play-ai-tool-toggle";
    // CSS-drawn triangle, rotated when open (see .play-ai-tool-toggle).
    head.append(toggle);
    const details = document.createElement("div");
    details.className = TOOL_DETAILS_BODY_CLASS;
    details.hidden = true;
    details.append(buildIoRow(IO_TAG_IN, raw).row);
    const out = buildIoRow(IO_TAG_OUT, "");
    out.row.hidden = true; // shown when the result (or an error) lands
    details.append(out.row);
    // Direct refs (not querySelector): nested verifier rows land inside
    // this line element, so a selector could match a child's blocks.
    line._outRow = out.row;
    line._outPre = out.pre;
    line.append(details);
    toggle.addEventListener("click", () => {
      const open = details.hidden;
      details.hidden = !open;
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

// A delegate verdict opens with "holds" or "refuted" (enforced by the
// verifier prompt). Classify off that first word so the row can flag the
// outcome at a glance; null when the output isn't a verdict.
const VERDICT_REFUTED = "refuted";
const VERDICT_HOLDS = "holds";

// Set on a withheld refutation the engine overruled: the move held.
const MOVE_SURVIVED_KEY = "move_survived";

function verdictKind(output) {
  if (output && typeof output === "object" && output[MOVE_SURVIVED_KEY]) return VERDICT_HOLDS;
  const verdict = output && typeof output === "object" ? output.verdict : null;
  if (typeof verdict !== "string") return null;
  const head = verdict.trimStart().toLowerCase();
  if (head.startsWith(VERDICT_REFUTED)) return VERDICT_REFUTED;
  if (head.startsWith(VERDICT_HOLDS)) return VERDICT_HOLDS;
  return null;
}

// The delegate row's dot carries this class; only that row bears a
// verdict, so nested sub-operations are never badged even if a future
// tool happens to return a `verdict` key.
const DELEGATE_DOT_CLASS = toolDotClass(TOOL.DELEGATE);

// Strip the leading holds/refuted token plus its trailing punctuation and
// space, so the prose reads as analysis once the word moves to the summary:
// "Refuted. After 11...O-O" -> "After 11...O-O".
const VERDICT_LEAD_RE = new RegExp(`^\\s*(?:${VERDICT_REFUTED}|${VERDICT_HOLDS})\\b[.,:;\\s-]*`, "i");
function stripVerdictLead(verdict) {
  return verdict.replace(VERDICT_LEAD_RE, "").trim();
}

// Verdict prose block: the verifier's reply shown with the same visuals as a
// self-correction (revision) -- accent border-left, accent summary caret,
// warm draft panel -- under its "Verifying line" row instead of buried as
// raw JSON in OUT. Foldable, collapsed by default. Idempotent: replay
// re-dispatches the same output. Summary is the verdict word (Holds/
// Refuted); the folded body is the prose with that word stripped.
const VERDICT_PROSE_CLASS = "play-ai-verdict";

function attachVerdictProse(line, output) {
  // Delegate rows only -- same scope as the verdict badge, so a future tool
  // that happens to return a `verdict` key never grows a prose panel.
  if (!hasOwnDot(line, DELEGATE_DOT_CLASS)) return;
  const verdict = output && typeof output === "object" ? output.verdict : null;
  if (typeof verdict !== "string" || !verdict.trim()) return;
  if (line.querySelector(`:scope > .${VERDICT_PROSE_CLASS}`)) return;
  const kind = verdictKind(output);
  // Drop the leading verdict word (now the summary) so the prose isn't
  // redundant: "Refuted. After 11...O-O" -> "After 11...O-O". A bare verdict
  // ("holds") leaves nothing to fold: the row badge says it all.
  const prose = kind ? stripVerdictLead(verdict) : verdict.trim();
  if (!prose) return;
  const details = document.createElement("details");
  details.className = `${REVISION_CLASS} ${VERDICT_PROSE_CLASS}`;
  const summary = document.createElement("summary");
  summary.className = REVISION_SUMMARY_CLASS;
  summary.textContent = kind ? capLead(kind) : "Verdict";
  const body = document.createElement("div");
  body.className = REVISION_BODY_CLASS;
  const para = document.createElement("p");
  para.className = PROSE_CLASS;
  para.textContent = prose;
  body.append(para);
  details.append(summary, body);
  line.append(details);
}

// Mark the "Verifying line" row with its verdict (refuted / holds) via a
// class the CSS badges. Idempotent -- replay re-dispatches the same
// output, so a second call must not stack badges.
function markVerdict(line, kind) {
  if (!kind) return;
  // Scoped to this row's own head (matching the CSS) so a nested child
  // delegate dot can't make a non-delegate parent row get badged.
  if (!hasOwnDot(line, DELEGATE_DOT_CLASS)) return;
  const cls = toolCallStateClass(kind);
  if (line.classList.contains(cls)) return;
  line.classList.add(cls);
}

// position_judge's OUT is "cleared N/M: ..." (see _judge_summary server-
// side). Its dot is orange by default (tool identity color), which reads as
// a failure next to the actual danger-red used for failed/refuted calls.
// Badge it green once something was actually cleared, matching the
// verdict-badge pattern above.
const POSITION_JUDGE_DOT_CLASS = toolDotClass(TOOL.POSITION_JUDGE);
const CLEARED_PREFIX_RE = /^cleared (\d+)\/\d+/;

function markCleared(line, output) {
  if (typeof output !== "string") return;
  if (!hasOwnDot(line, POSITION_JUDGE_DOT_CLASS)) return;
  const match = CLEARED_PREFIX_RE.exec(output);
  if (!match || match[1] === "0") return;
  line.classList.add(toolCallStateClass("cleared"));
}

// Fill the OUT row with the tool result from ai_tool_call_complete.
// Idempotent (replay-on-reconnect re-dispatches with the same output).
export function setAiToolCallResult({ toolUseId, output }) {
  if (!inst.body || !toolUseId || output === undefined) return;
  const line = inst.body._toolCallNodes.get(toolUseId);
  if (!line || !line._outPre) return;
  withStickyBottom(() => {
    line._outPre.textContent = formatToolOutput(output);
    line._outRow.hidden = false;
    markVerdict(line, verdictKind(output));
    attachVerdictProse(line, output);
    markCleared(line, output);
  });
}

// Errors that mean "the tool ran, the loop is steering the model" rather
// than "the tool failed" -- struck through, but not marked red. Keyed by
// error code so the rule is the same for every tool that returns one.
const SUPERSEDED_ERRORS = new Set(["recommendation_rejected", "red_team_first"]);

export function markAiToolCallFailed({ toolUseId, error, detail }) {
  if (!inst.body || !toolUseId) return;
  const line = inst.body._toolCallNodes.get(toolUseId);
  if (!line) return;
  const cls = toolCallStateClass(SUPERSEDED_ERRORS.has(error) ? "superseded" : "failed");
  // Idempotent: replay-on-reconnect can re-dispatch this event for the same
  // row; appending the suffix/gear twice would stack them.
  if (line.classList.contains(cls)) return;
  line.classList.add(cls);
  if (!line._outPre) return;
  // The complete event precedes failed and already fills OUT with the
  // error dict; only set the text when it is still empty (old events).
  if (!line._outPre.textContent) {
    line._outPre.textContent = detail ? `${error}: ${detail}` : error;
  }
  // Same inline action (gear -> Settings) the toast path uses.
  for (const a of errorActionsFor(error)) {
    line._outPre.append(" ", buildToastActionButton(a));
  }
  line._outRow.hidden = false;
}

// Cap for the joined item list in a multi-item fallback (keeps the collapsed
// summary to one line). Items past it drop whole, never cut mid-quote.
const REVISION_ITEMS_MAX = 48;
const ITEMS_SEP = ", ";
const ELLIPSIS = "...";
const QUOTE = "\"";

// Self-correction phrasings, picked from so the revision summary doesn't read
// robotically. Each row is [no-items, one, many]; "{}" is the item slot, and
// every opener is distinct. The original wording is the first row. Pick is
// deterministic on the round (stable across panel rehydration -- never random).
const REVISION_PHRASES = [
  ["Actually, let me reconsider.", "Actually, {} isn't right.",  "Wait, {} may be wrong."],
  ["Scratch that.",               "Scratch that -- {} is wrong.", "Hold on -- {} are off."],
  ["Let me correct myself.",      "Correcting myself: {} is off.", "My mistake -- {} are wrong."],
  ["One moment.",                 "I had {} wrong.",              "{} -- incorrect."],
  ["Let me back up.",             "{} is nonsense.",              "{} are suspect."],
  ["Rethinking this.",           "Strike {}; it's wrong.",       "Strike {}; they're wrong."],
];

// A SAN move ("Nab1", "Qd1", "O-O") whose leading capital is the piece letter
// and must be kept. Castling and a piece letter + SAN body char.
const SAN_LEAD_RE = /^(?:O-O|[KQRBN][a-h1-8x])/;

// A lowercase chess move whose case is meaningful and must be kept -- a pawn
// push or capture ("e4", "exd5", "fxg1=Q"). Distinct from SAN_LEAD_RE (which
// is piece moves); a pawn move starts with a file letter.
const PAWN_MOVE_RE = /^[a-h](?:[1-8]|x[a-h][1-8])/;

// Capitalize a prose item at a sentence start ("white bishop" -> "White
// bishop"); leave chess moves untouched so "e4"/"Nf3" keep their case.
function capLead(s) {
  if (SAN_LEAD_RE.test(s) || PAWN_MOVE_RE.test(s)) return s;
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Deterministic pick from `pool` by `seed` (the round), stable across panel
// rehydration on reconnect -- never random.
function pickBySeed(pool, seed) {
  const n = pool.length;
  return pool[((seed % n) + n) % n];
}

// Items are verbatim quotes, so they drop in as-is at any position.
function fill(template, joined) {
  return template.replace("{}", joined);
}

// Join whole items up to `max` chars; the first always fits, the rest that
// don't collapse to a trailing ellipsis.
function joinCapped(items, max) {
  let joined = items[0];
  for (const item of items.slice(1)) {
    const next = joined + ITEMS_SEP + item;
    if (next.length > max) return joined + ITEMS_SEP + ELLIPSIS;
    joined = next;
  }
  return joined;
}

// The flagged spans as quotes: prose case (surfaces may arrive lowercased),
// prose order, deduped. A surface the prose never matched is quoted as sent.
function quoteItems(struck, surfaces) {
  const byKey = new Map();
  for (const s of [...struck, ...surfaces]) {
    const key = s.toLowerCase();
    if (!byKey.has(key)) byKey.set(key, s);
  }
  return [...byKey.values()].map((s) => QUOTE + s + QUOTE);
}

// Fallback summary when the next round has no usable opening line: the AI
// catching its own slip, quoting what it got wrong.
function revisionFallbackText(items, seed = 0) {
  const [none, one, many] = pickBySeed(REVISION_PHRASES, seed);
  if (!items.length) return none;
  if (items.length === 1) return fill(one, items[0]);
  return fill(many, joinCapped(items, REVISION_ITEMS_MAX));
}

// Weak models leak a standalone acknowledgment ("Understood.", "You're
// right,") as the opening prose despite the prompt forbidding it. Replace that
// lead with a board-oriented opener so the prose reads as analysis, not
// compliance. Matches the phrase then its terminator -- period, exclamation,
// comma, or em/en dash -- or an "..., I'll ..." continuation; never a bare run
// of text that could be real analysis. The terminator is captured and
// re-appended so the original punctuation (and sentence flow) is preserved:
// "Understood, x" keeps its comma. The apostrophe class covers straight and
// curly quotes (U+2018/U+2019) since models emit both for "you're".
// Straight or curly apostrophe -- models emit both in contractions.
const APOS = "['\\u2018\\u2019]";
// Sentence terminator the ack lead ends on, including em/en dash (models
// write "You're correct--I misread" with a dash, no period).
const ACK_TERM = "[.!,\\u2013\\u2014]";
// "you're right" / "you are correct" etc.: full or contracted "are", and
// either affirmation word.
const YOU_ARE = `you(?:${APOS}re| are)`;
const ACK_PHRASE = `understood|i understand|got it|${YOU_ARE} (?:right|correct)`;
const ACK_LEAD_RE = new RegExp(
  `^(?:${ACK_PHRASE})(?:,?\\s+i(?:${APOS}ll| will)[^.!?]*)?(${ACK_TERM}) *`, "i",
);
// Bare clauses -- no trailing punctuation; the captured terminator is appended.
const ACK_OPENERS = [
  "Now I see the board",
  "Looking at the position",
  "Reading the position",
  "Here is the position",
  "Assessing the position",
];

// Replace a leaked acknowledgment opener on the accumulated prose. Operates on
// the whole paragraph text (deltas may split the ack), idempotent -- once the
// ack is gone the regex no longer matches. `seed` (round) varies the opener.
// A dash binds tight to the next word ("position--I"), so no trailing space
// after it; comma/period keep theirs.
const ACK_DASH_RE = new RegExp("[\\u2013\\u2014]");
function scrubAckLead(para, seed) {
  const text = para.textContent;
  if (!ACK_LEAD_RE.test(text)) return;
  para.textContent = text.replace(ACK_LEAD_RE, (_m, term) => {
    const tail = ACK_DASH_RE.test(term) ? "" : " ";
    return pickBySeed(ACK_OPENERS, seed) + term + tail;
  });
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Cross out each flagged token in the round's prose. Rebuilds the paragraph
// with matched spans wrapped in <del> so the reader sees the self-correction
// land on the actual text. Longest items first so a line isn't half-matched.
// Returns the struck spans as written, in prose order.
function strikeProseItems(para, items) {
  const text = para.textContent;
  if (!text || !items.length) return [];
  const sorted = [...items].sort((a, b) => b.length - a.length);
  // Case-insensitive: server lowercases flagged claims, but the prose keeps
  // its original case ("Knight on b1" at a sentence start).
  const re = new RegExp(sorted.map(escapeRegExp).join("|"), "gi");
  para.textContent = "";
  const struck = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) para.append(document.createTextNode(text.slice(last, m.index)));
    const del = document.createElement("del");
    del.className = "play-ai-prose-struck";
    del.textContent = m[0];
    para.append(del);
    struck.push(m[0]);
    last = m.index + m[0].length;
  }
  if (last < text.length) para.append(document.createTextNode(text.slice(last)));
  return struck;
}

export function noteAiPosition({ round, surfaces, hideProse = false }) {
  if (!inst.body) return;
  const entry = inst.body._roundPanels.get(round);
  if (!entry || !entry.revision) return;
  // Tool-name leak: process talk, not a chess slip -- withhold the prose
  // outright, no self-correction. The next round restates it cleanly.
  if (hideProse) {
    entry.para.hidden = true;
    return;
  }
  if (!surfaces || !surfaces.length) return;
  // Strike the flagged spans (exact prose), then tuck the flawed prose into
  // the revision body so the clean (next-round) prose reads on its own.
  // The summary is a canned self-correction line quoting those spans.
  const struck = strikeProseItems(entry.para, surfaces);
  entry.revision.summary.textContent = revisionFallbackText(quoteItems(struck, surfaces), round);
  entry.revision.body.append(entry.para);
  entry.revision.details.hidden = false;
}

export function setAiStatus(state) {
  // `state` in: idle | waiting | engine | done.
  // Spinner shows on waiting/engine; hidden on idle/done. First text
  // delta no longer hides the status (the header stays as a label).
  if (!inst.body) return;
  const text = STATUS_TEXT[state] ?? "";
  if (state === AI_STATE.IDLE || !text) {
    inst.body._status.hidden = true;
    inst.body._statusText.textContent = "";
    return;
  }
  inst.body._status.hidden = false;
  inst.body._statusText.textContent = text;
  inst.body._spinner.style.display = (state === AI_STATE.DONE) ? "none" : "";
}

export function appendAiDelta(text, roundIndex = 0, thinkingMs = null) {
  if (!inst.body || !text) return;
  withStickyBottom(() => {
    const entry = ensureRoundPanel(inst.body, roundIndex);
    // Freeze before stripping: thinking_ms rides the first prose delta,
    // which may be whitespace-only. Dropping it with the whitespace loses
    // the server duration, collapsing the label to "1s" on replay.
    if (!entry.hasProse) freezeThinkingLabel(entry, thinkingMs);
    // Drop leading whitespace until the first non-ws char arrives;
    // prevents an empty-looking bordered box on rounds whose prose
    // starts with stray newlines from the model.
    const out = entry.hasProse ? text : text.replace(/^\s+/, "");
    if (!out) return;
    entry.hasProse = true;
    entry.para.append(document.createTextNode(out));
    // Strip a leaked "Understood."-style ack opener once enough text has
    // landed to recognize it (the ack may span deltas).
    scrubAckLead(entry.para, roundIndex);
  });
}

// A round-cap note with a gear that opens Settings -> Analysis. Shared by
// the narrator tool-call cap and the verifier-rounds cap (same tab).
function _roundCapNote(message) {
  const note = document.createElement("div");
  note.className = ROUNDCAP_CLASS;
  const text = document.createElement("span");
  text.textContent = message;
  const gear = document.createElement("wa-icon");
  gear.name = "gear";
  gear.className = "play-ai-roundcap-gear";
  gear.setAttribute("role", "button");
  gear.setAttribute("tabindex", "0");
  gear.setAttribute("aria-label", "Open AI settings");
  const open = () => openSettings(SETTINGS_TAB_ANALYSIS);
  gear.addEventListener("click", open);
  gear.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
  });
  note.append(text, gear);
  return note;
}

function _terminalNote(message) {
  const note = document.createElement("div");
  note.className = ROUNDCAP_CLASS;
  note.textContent = message;
  return note;
}

// `rounds` is the server's count (silent rounds leave no panel to count).
function noRecommendationText(rounds) {
  const after = rounds === null ? "" : ` after ${rounds} ${rounds === 1 ? "round" : "rounds"}`;
  return `No move chosen -- the analysis finished${after} without committing to one.`;
}

export function markAiDone({
  cancelled = false,
  error = null,
  errorDetail = null,
  roundCap = false,
  verifierRoundCap = false,
  noResponse = false,
  noRecommendation = false,
  rounds = null,
  usage = null,
  provider = null,
} = {}) {
  // Terminal: switch the header text + drop the spinner. Markers (error >
  // roundCap > noResponse > noRecommendation > cancelled if any apply)
  // land in the dedicated terminal slot below the last round panel.
  const naturalCompletion =
    !error && !roundCap && !noResponse && !noRecommendation && !cancelled;
  setAiStatus(naturalCompletion ? AI_STATE.DONE : AI_STATE.IDLE);
  if (!inst.body) return;
  // Refresh the ticker from the done totals: a replay that delivers only
  // the terminal event (no ai_usage stream) still restores the count.
  if (usage) setAiUsage(usage);
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
      // Skip when the last round's prose was folded into its revision -- the
      // border would land on text tucked inside the collapsed disclosure.
      const folded = last && last.para.parentNode === last.revision?.body;
      if (last && !folded && !last.para.hidden) last.para.classList.add("play-ai-prose-final");
    }
    // Token breakdown first (before the marker blocks' early returns) so
    // it renders on every terminal path that keeps the panel alive.
    if (usage && usageTotal(usage) > 0) {
      const line = document.createElement("div");
      line.className = "play-ai-usage";
      line.textContent = fmtUsageSummary(usage, provider);
      slot.append(line);
    }
    if (error) {
      const block = document.createElement("div");
      block.className = ERROR_CLASS;
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
    // Advisory: a verification step capped out. The main analysis may have
    // completed fine, so render the note without returning -- it sits above
    // any other terminal marker below.
    if (verifierRoundCap) {
      slot.append(_roundCapNote(
        "A verification step stopped early at its round cap. Raise \"Max subagent rounds\" for fuller checks",
      ));
    }
    if (roundCap) {
      slot.append(_roundCapNote(
        "Stopped early at the tool-call cap. Raise \"Max rounds\" to allow more rounds.",
      ));
      return;
    }
    if (noResponse) {
      slot.append(_terminalNote(
        "Model produced no answer. Try a different model -- some stream only chain-of-thought.",
      ));
      return;
    }
    if (noRecommendation) {
      slot.append(_terminalNote(noRecommendationText(rounds)));
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
