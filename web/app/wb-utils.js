// Read a px-valued CSS custom property from the element matching
// `selector` (default: document root), with a numeric fallback when the
// var is unset, zero, or unparsable.
export function cssVarPx(name, fallback, selector) {
  const el = (selector && document.querySelector(selector)) || document.documentElement;
  const v = parseFloat(getComputedStyle(el).getPropertyValue(name));
  return v > 0 ? v : fallback;
}

// Ribbon strip width; matches the `--ribbon-w` CSS var on the
// perspective's grid/body. Callers decide which edge(s) to reserve.
const RIBBON_W_FALLBACK_PX = 36;
export function ribbonWidthPx(selector) {
  return cssVarPx("--ribbon-w", RIBBON_W_FALLBACK_PX, selector);
}

// Coalesce repeated calls into one requestAnimationFrame: however many
// times the returned schedule() fires before the frame, fn runs once.
// cancel() drops a pending frame.
export function rafCoalesce(fn) {
  let id = 0;
  const schedule = () => {
    if (!id) id = requestAnimationFrame(() => { id = 0; fn(); });
  };
  schedule.cancel = () => { if (id) { cancelAnimationFrame(id); id = 0; } };
  return schedule;
}

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

// Format an engine score for display. Options cover the per-site variants:
//   empty       text for a missing score ("" or "--")
//   matePrefix  glyph before a mate count ("#" or "M")
//   signed      prepend "+" to non-negative centipawn scores
export function fmtScore(score, { empty = "", matePrefix = "#", signed = false } = {}) {
  if (!score) return empty;
  if (score.mate != null) return `${matePrefix}${score.mate}`;
  if (score.cp != null) {
    const sign = signed && score.cp >= 0 ? "+" : "";
    return `${sign}${(score.cp / 100).toFixed(2)}`;
  }
  return empty;
}

// Humanize a count (nodes, nps): >=1M as "1.20M", >=1K as "12K", else raw.
// null/undefined -> "" so callers can blank a missing field.
export function fmtCount(n) {
  if (n == null) return "";
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
}

// Format a clock value (seconds) as m:ss, or as tenths below tenthsBelow
// seconds so bullet/sub-second-increment games stay readable. Negatives
// clamp to 0; a non-finite value yields `invalid`.
export function fmtClock(seconds, { tenthsBelow = 10, invalid = "—" } = {}) {
  if (!Number.isFinite(seconds)) return invalid;
  const t = Math.max(0, seconds);
  if (t < tenthsBelow) return t.toFixed(1);
  const s = Math.floor(t);
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return `${m}:${ss.toString().padStart(2, "0")}`;
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

export function isPinnedToRight(scroller, slack) {
  if (!scroller) return false;
  return scroller.scrollWidth - scroller.scrollLeft - scroller.clientWidth <= slack;
}

export function scrollToRight(scroller) {
  if (!scroller) return;
  scroller.scrollLeft = scroller.scrollWidth;
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
