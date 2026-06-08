export function flashWindow(wb) {
  wb.addClass("wb-attention");
  wb.g.addEventListener("animationend", () => wb.removeClass("wb-attention"), { once: true });
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Trailing-edge debounce: collapse bursts to one call ms after the last.
export function debounce(fn, ms) {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
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

// Make Ctrl/Cmd+A inside `el` select just one node's contents instead
// of the whole page. `targetFn(ev)` returns the node to select (default
// `el`); returning falsy leaves the selection untouched.
export function selectContentsOnCtrlA(el, targetFn = () => el) {
  el.tabIndex = 0;
  el.addEventListener("keydown", (ev) => {
    if (!((ev.ctrlKey || ev.metaKey) && (ev.key === "a" || ev.key === "A"))) return;
    ev.preventDefault();
    const target = targetFn(ev);
    if (!target) return;
    const sel = window.getSelection();
    sel.removeAllRanges();
    const range = document.createRange();
    range.selectNodeContents(target);
    sel.addRange(range);
  });
}
