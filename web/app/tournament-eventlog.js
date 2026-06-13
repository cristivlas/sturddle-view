// Tournament event-log list renderer. Turns the captured event log (entries
// shaped by tournament-live-state's addLogEntry) into <li> rows. Shared by
// the Arena workspace (which wraps it with an error banner) and Studio.

import { EVT, KIND } from "./tournament-events.js";
import { AUTOSCROLL_SLACK_ROW_PX, escapeHtml, isPinnedToBottom, scrollToBottom } from "./wb-utils.js";

// Render eventLog into listEl. scroller (the scrollable ancestor) keeps the
// view pinned to the bottom across appends when the user is already there.
export function renderEventLogList(listEl, eventLog, scroller) {
  if (!listEl) return;
  const atBottom = !scroller || isPinnedToBottom(scroller, AUTOSCROLL_SLACK_ROW_PX);
  listEl.innerHTML = eventLog.filter(e => e.payload?.kind !== KIND.PROXY_UNPAIRED).map((e) => {
    const ts = e.ts || "";
    const inner = e.payload?.kind;
    // runner_log: surface the actual fastchess stdout/stderr line.
    if (inner === KIND.RUNNER_LOG && e.payload?.line) {
      const stream = e.payload.stream === "err" ? " err" : "";
      return `<li><span class="wb-log-ts">${ts}</span>` +
        `<span class="wb-log-runner${stream}">${escapeHtml(e.payload.line)}</span></li>`;
    }
    // Muted detail parts appended after the primary kind label.
    const parts = [];
    if (e.kind === EVT.STATUS && e.payload?.status)
      parts.push(e.payload.status);
    else if (inner === KIND.GAME_FINISHED) {
      const a = e.payload?.engine_a || "?";
      const b = e.payload?.engine_b || "?";
      const result = e.payload?.result;
      const termination = e.payload?.termination;
      const tail = (result && termination && termination !== "unknown")
        ? `${result} ${termination}` : (result || "");
      const gn = e.payload?.game_n;
      const head = (gn != null) ? `${inner} #${gn}` : inner;
      parts.push(head, `${a} vs ${b}`, ...(tail ? [tail] : []));
    } else if (inner === KIND.PROXY_PAIRED) {
      const a = e.payload?.engine_a || "?";
      const b = e.payload?.engine_b || "?";
      const pa = e.payload?.proxy_a || "";
      const pb = e.payload?.proxy_b || "";
      parts.push(inner, `${a}(${pa}) vs ${b}(${pb})`);
    } else if (inner === KIND.PROXY_STARTED) {
      parts.push(inner);
      if (e.payload?.engine_name) parts.push(e.payload.engine_name);
    } else if (inner === KIND.RUNNER_CRASH) {
      parts.push(inner);
      if (e.payload?.rc != null) parts.push(`rc=${e.payload.rc}`);
    } else if (inner) {
      parts.push(inner);
    }
    const detailHtml = parts.map(p => ` <span class="wb-log-detail">${escapeHtml(p)}</span>`).join("");
    return `<li><span class="wb-log-ts">${ts}</span> <span class="wb-log-kind">${escapeHtml(e.kind)}</span>${detailHtml}</li>`;
  }).join("");
  if (atBottom && scroller) scrollToBottom(scroller);
}
