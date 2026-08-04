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
const THINKING_OPEN_KEY = STORAGE_KEY.AI_THINKING_OPEN;

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

function readThinkingOpen() {
  return loadRaw(THINKING_OPEN_KEY) === "1";
}

function writeThinkingOpen(open) {
  saveRaw(THINKING_OPEN_KEY, open ? "1" : "0");
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
    const errorBlock = hovered?.closest(".play-ai-error");
    const prosePara = hovered?.closest(".play-ai-prose");
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
  para.className = "play-ai-prose";
  // Revision: a collapsible that folds away prose the AI corrected. The
  // summary carries a self-correction line; the body holds the struck-through
  // flawed prose.
  const revision = document.createElement("details");
  revision.className = "play-ai-revision";
  revision.hidden = true;
  const revisionSummary = document.createElement("summary");
  revisionSummary.className = "play-ai-revision-summary";
  const revisionBody = document.createElement("div");
  revisionBody.className = "play-ai-revision-body";
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
  report_line:    "Checking line",
  related_openings: "Comparing openings",
};

// Tools whose label shows the actual move under consideration ("Considering
// Nd3"). Maps the tool to the verb; the move SAN from input.move is appended.
const MOVE_TOOL_VERBS = {
  recommend_move: "Considering",
  validate_move:  "Validating",
  delegate:       "Verifying",
};

const ANALYSIS_WINDOW_TITLE = "Analysis";

const TOOL_DETAILS_BODY_CLASS = "play-ai-tool-details-body";
const ROUNDCAP_CLASS = "play-ai-roundcap";

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

function friendlyToolLabel(name, input) {
  const move = input && typeof input.move === "string" ? input.move.trim() : "";
  const verb = MOVE_TOOL_VERBS[name];
  if (verb && move) return `${verb} ${move}`;
  return TOOL_FRIENDLY_LABELS[name] || name;
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
  defaultW: () => 380,
  defaultH: 300,
  defaultY: () => 100,
  build() {
    return buildBody();
  },
  dockOrder: DOCK_ORDER.AI_ANALYSIS,
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
  if (inst.wb || inst.slot || inst.inlineSlot) return;
  inst.toggle(null);
}

export function closeAi() {
  inst.close();
}

export function isAiOpen() {
  return !!(inst.wb || inst.slot || inst.inlineSlot);
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
  inst.body._currentRound = null;
  setAiStatus("waiting");
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
      entry.thinking.summary.classList.add("is-active");
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
      freezeThinkingLabel(entry, thinkingMs);
      container = entry.tools;
    }
    // Dot/label/arrow live in a nowrap head that scrolls horizontally,
    // so a long label never wraps the arrow onto its own line.
    const head = document.createElement("div");
    head.className = "play-ai-tool-head";
    line.append(head);
    const dot = document.createElement("span");
    dot.className = `play-ai-tool-dot play-ai-tool-dot-${name}`;
    head.append(dot);
    const label = document.createElement("span");
    label.className = "play-ai-tool-label";
    label.textContent = friendlyToolLabel(name, input);
    head.append(label);
    const args = formatToolArgs(input);
    const raw = args ? `${name}(${args})` : `${name}()`;
    const toggle = document.createElement("span");
    toggle.className = "play-ai-tool-toggle";
    // One glyph, rotated via CSS when open -- guarantees the open/closed
    // caret are identical size (the unicode triangles aren't).
    toggle.textContent = "\u25b6";
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

function verdictKind(output) {
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
const DELEGATE_DOT_CLASS = "play-ai-tool-dot-delegate";

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
  if (!line.querySelector(`:scope > .play-ai-tool-head > .${DELEGATE_DOT_CLASS}`)) return;
  const verdict = output && typeof output === "object" ? output.verdict : null;
  if (typeof verdict !== "string" || !verdict.trim()) return;
  // Server flagged the prose as possibly wrong (a non-LLM validator hit): hide
  // it entirely rather than risk a false claim. The badge and raw OUT stay.
  if (output.prose_flagged) return;
  if (line.querySelector(`:scope > .${VERDICT_PROSE_CLASS}`)) return;
  const kind = verdictKind(output);
  const details = document.createElement("details");
  details.className = `play-ai-revision ${VERDICT_PROSE_CLASS}`;
  const summary = document.createElement("summary");
  summary.className = "play-ai-revision-summary";
  summary.textContent = kind ? capLead(kind) : "Verdict";
  const body = document.createElement("div");
  body.className = "play-ai-revision-body";
  const para = document.createElement("p");
  para.className = "play-ai-prose";
  // Drop the leading verdict word (now the summary) so the prose isn't
  // redundant: "Refuted. After 11...O-O" -> "After 11...O-O".
  para.textContent = kind ? stripVerdictLead(verdict) : verdict.trim();
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
  if (!line.querySelector(`:scope > .play-ai-tool-head > .${DELEGATE_DOT_CLASS}`)) return;
  const cls = `play-ai-tool-call-${kind}`;
  if (line.classList.contains(cls)) return;
  line.classList.add(cls);
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
  const cls = SUPERSEDED_ERRORS.has(error)
    ? "play-ai-tool-call-superseded"
    : "play-ai-tool-call-failed";
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

// Cap for the joined item list in a multi-item fallback before it is
// ellipsis-trimmed (keeps the collapsed summary to one line).
const REVISION_ITEMS_MAX = 48;

// Self-correction phrasings, picked from so the revision summary doesn't read
// robotically. Each row is [no-items, one, many]; "{}" is the item slot, and
// every opener is distinct. The original wording is the first row. Pick is
// deterministic on the round (stable across panel rehydration -- never random).
const REVISION_PHRASES = [
  ["Actually, let me reconsider.", "Actually, {} isn't right.",  "Wait, {} look wrong."],
  ["Scratch that.",               "Scratch that -- {} is wrong.", "Hold on -- {} are off."],
  ["Let me correct myself.",      "Correcting myself: {} is off.", "My mistake -- {} are wrong."],
  ["One moment.",                 "I had {} wrong.",              "{} -- incorrect."],
  ["Let me back up.",             "{} is nonsense.",              "{} are suspect."],
  ["Rethinking this.",           "Strike {}; it's wrong.",       "Strike {}; they're wrong."],
];

// A SAN move ("Nab1", "Qd1", "O-O") whose leading capital is the piece letter
// and must be kept; prose claims ("White's bishop on g3") read better with the
// lead lowercased mid-sentence. Castling and a piece letter + SAN body char.
const SAN_LEAD_RE = /^(?:O-O|[KQRBN][a-h1-8x])/;

// A lowercase chess move whose case is meaningful and must be kept -- a pawn
// push or capture ("e4", "exd5", "fxg1=Q"). Distinct from SAN_LEAD_RE (which
// is piece moves); a pawn move starts with a file letter.
const PAWN_MOVE_RE = /^[a-h](?:[1-8]|x[a-h][1-8])/;

// Lowercase the first letter of a prose item so it reads mid-sentence
// ("White's bishop" -> "white's bishop"); leave SAN moves untouched.
function decapLead(s) {
  return SAN_LEAD_RE.test(s) ? s : s.charAt(0).toLowerCase() + s.slice(1);
}

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

// Case the item to its position: capitalized when "{}" leads the phrase
// (sentence start), lowercased when a prefix precedes it. Chess moves keep
// their case either way.
function fill(template, joined) {
  const lead = template.startsWith("{}");
  return template.replace("{}", lead ? capLead(joined) : decapLead(joined));
}

// Fallback summary when the next round has no usable opening line: the AI
// catching its own slip. SAN moves keep their case so "Nab1" isn't mangled.
function revisionFallbackText(items, seed = 0) {
  const [none, one, many] = pickBySeed(REVISION_PHRASES, seed);
  if (!items.length) return none;
  if (items.length === 1) return fill(one, items[0]);
  let joined = items.join(", ");
  if (joined.length > REVISION_ITEMS_MAX) {
    joined = joined.slice(0, REVISION_ITEMS_MAX).trimEnd() + "...";
  }
  return fill(many, joined);
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
function strikeProseItems(para, items) {
  const text = para.textContent;
  if (!text || !items.length) return;
  const sorted = [...items].sort((a, b) => b.length - a.length);
  // Case-insensitive: server lowercases flagged claims, but the prose keeps
  // its original case ("Knight on b1" at a sentence start).
  const re = new RegExp(sorted.map(escapeRegExp).join("|"), "gi");
  para.textContent = "";
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) para.append(document.createTextNode(text.slice(last, m.index)));
    const del = document.createElement("del");
    del.className = "play-ai-prose-struck";
    del.textContent = m[0];
    para.append(del);
    last = m.index + m[0].length;
  }
  if (last < text.length) para.append(document.createTextNode(text.slice(last)));
}

export function noteAiPosition({ round, surfaces }) {
  if (!inst.body) return;
  const entry = inst.body._roundPanels.get(round);
  if (!entry || !entry.revision) return;
  if (!surfaces || !surfaces.length) return;
  // Strike the flagged spans (exact prose), then tuck the flawed prose into
  // the revision body so the clean (next-round) prose reads on its own.
  // The summary is the canned self-correction line.
  strikeProseItems(entry.para, surfaces);
  entry.revision.summary.textContent = revisionFallbackText(surfaces, round);
  entry.revision.body.append(entry.para);
  entry.revision.details.hidden = false;
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


export function markAiDone({
  cancelled = false,
  error = null,
  errorDetail = null,
  roundCap = false,
  verifierRoundCap = false,
  noResponse = false,
  noRecommendation = false,
  usage = null,
  provider = null,
} = {}) {
  // Terminal: switch the header text + drop the spinner. Markers (error >
  // roundCap > noResponse > noRecommendation > cancelled if any apply)
  // land in the dedicated terminal slot below the last round panel.
  const naturalCompletion =
    !error && !roundCap && !noResponse && !noRecommendation && !cancelled;
  setAiStatus(naturalCompletion ? "done" : "idle");
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
      if (last && !folded) last.para.classList.add("play-ai-prose-final");
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
      const note = document.createElement("div");
      note.className = ROUNDCAP_CLASS;
      note.textContent = "Model produced no answer. Try a different model -- some stream only chain-of-thought.";
      slot.append(note);
      return;
    }
    if (noRecommendation) {
      const note = document.createElement("div");
      note.className = ROUNDCAP_CLASS;
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
