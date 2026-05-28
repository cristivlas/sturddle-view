export function flashWindow(wb) {
  wb.addClass("wb-attention");
  wb.g.addEventListener("animationend", () => wb.removeClass("wb-attention"), { once: true });
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Sticky-bottom autoscroll: snapshot pinned BEFORE mutating, restore
// after, so a user who scrolled up to inspect earlier content is not
// yanked back down by new content.
//
// Slack = px from bottom that still counts as "at bottom". Pick by
// content cadence: prose tokens overshoot fast, single lines barely.
export const AUTOSCROLL_SLACK_PROSE_PX = 24;  // multi-line streaming prose
export const AUTOSCROLL_SLACK_LINE_PX = 4;    // single line per event (UCI log)
export const AUTOSCROLL_SLACK_ROW_PX = 40;    // table-row re-renders

export function isPinnedToBottom(scroller, slack) {
  if (!scroller) return false;
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= slack;
}

export function scrollToBottom(scroller) {
  if (!scroller) return;
  scroller.scrollTop = scroller.scrollHeight;
}
