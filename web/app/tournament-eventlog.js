// Tournament event-log formatting: the captured event log (entries shaped by
// tournament-live-state's addLogEntry) as <li> rows or as copyable plain text.
// Shared by the Arena workspace (which adds an error banner) and Studio.

import { EVT, KIND } from "./tournament-events.js";
import { AUTOSCROLL_SLACK_ROW_PX, escapeHtml, isPinnedToBottom, scrollToBottom } from "./wb-utils.js";

const RUNNER_ERR_STREAM = "err";
const UNKNOWN_TERMINATION = "unknown";
const UNKNOWN_ENGINE = "?";

const shownEntries = (eventLog) => eventLog.filter(e => e.payload?.kind !== KIND.PROXY_UNPAIRED);

// One entry's parts: a runner_log line (the actual fastchess stdout/stderr),
// or the event kind plus muted detail parts. The list and the text copy both
// read these, so the copied log says exactly what the list shows.
function entryParts(e) {
  const inner = e.payload?.kind;
  if (inner === KIND.RUNNER_LOG && e.payload?.line) {
    return { runner: e.payload.line, err: e.payload.stream === RUNNER_ERR_STREAM };
  }
  const details = [];
  if (e.kind === EVT.STATUS && e.payload?.status)
    details.push(e.payload.status);
  else if (inner === KIND.GAME_FINISHED) {
    const a = e.payload?.engine_a || UNKNOWN_ENGINE;
    const b = e.payload?.engine_b || UNKNOWN_ENGINE;
    const result = e.payload?.result;
    const termination = e.payload?.termination;
    const tail = (result && termination && termination !== UNKNOWN_TERMINATION)
      ? `${result} ${termination}` : (result || "");
    const gn = e.payload?.game_n;
    const head = (gn != null) ? `${inner} #${gn}` : inner;
    details.push(head, `${a} vs ${b}`, ...(tail ? [tail] : []));
  } else if (inner === KIND.PROXY_PAIRED) {
    const a = e.payload?.engine_a || UNKNOWN_ENGINE;
    const b = e.payload?.engine_b || UNKNOWN_ENGINE;
    const pa = e.payload?.proxy_a || "";
    const pb = e.payload?.proxy_b || "";
    details.push(inner, `${a}(${pa}) vs ${b}(${pb})`);
  } else if (inner === KIND.PROXY_STARTED) {
    details.push(inner);
    if (e.payload?.engine_name) details.push(e.payload.engine_name);
  } else if (inner === KIND.RUNNER_CRASH) {
    details.push(inner);
    if (e.payload?.rc != null) details.push(`rc=${e.payload.rc}`);
  } else if (inner) {
    details.push(inner);
  }
  return { kind: e.kind, details };
}

// Render eventLog into listEl. scroller (the scrollable ancestor) keeps the
// view pinned to the bottom across appends when the user is already there.
export function renderEventLogList(listEl, eventLog, scroller) {
  if (!listEl) return;
  const atBottom = !scroller || isPinnedToBottom(scroller, AUTOSCROLL_SLACK_ROW_PX);
  listEl.innerHTML = shownEntries(eventLog).map((e) => {
    const ts = e.ts || "";
    const p = entryParts(e);
    if (p.runner) {
      return `<li><span class="wb-log-ts">${ts}</span>` +
        `<span class="wb-log-runner${p.err ? " err" : ""}">${escapeHtml(p.runner)}</span></li>`;
    }
    const detailHtml = p.details.map(d => ` <span class="wb-log-detail">${escapeHtml(d)}</span>`).join("");
    return `<li><span class="wb-log-ts">${ts}</span> <span class="wb-log-kind">${escapeHtml(p.kind)}</span>${detailHtml}</li>`;
  }).join("");
  if (atBottom && scroller) scrollToBottom(scroller);
}

// The event log as plain text, one line per entry the list shows.
export function eventLogText(eventLog) {
  return shownEntries(eventLog).map((e) => {
    const ts = e.ts || "";
    const p = entryParts(e);
    return p.runner ? `${ts} ${p.runner}` : [ts, p.kind, ...p.details].join(" ");
  }).join("\n");
}
