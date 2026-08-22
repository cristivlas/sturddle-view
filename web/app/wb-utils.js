import { scrollRowIntoView } from "./col-sort.js";

// Last path segment (handles / and \); "" for empty input.
export function basename(p) {
  if (!p) return "";
  return String(p).replace(/[\\/]+$/, "").split(/[\\/]/).pop() || String(p);
}

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
const RIBBON_W_FALLBACK_PX = 44;
export function ribbonWidthPx(selector) {
  return cssVarPx("--ribbon-w", RIBBON_W_FALLBACK_PX, selector);
}

// Bottom edge of the nav header (its amber accent line) -- the top boundary
// for floating windows. The header is content-sized, so measure it.
const HEADER_H_FALLBACK_PX = 44;
export function headerBottomPx() {
  const h = document.querySelector("header");
  return h ? Math.round(h.getBoundingClientRect().bottom) : HEADER_H_FALLBACK_PX;
}

// Hover tooltip showing the full value on inputs that may overflow.
// Tracks user edits; returns the updater for programmatic value changes.
export function syncTooltip(input) {
  const update = () => { input.title = input.value || ""; };
  input.addEventListener("input", update);
  update();
  return update;
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

// Leading-edge throttle: fire immediately, swallow calls for ms after.
// For actions with external side effects (e.g. opening an OS window) where
// guard() alone can't help -- the call resolves before the next click lands.
export function cooldown(fn, ms) {
  let until = 0;
  return (...args) => {
    const now = performance.now();
    if (now < until) return;
    until = now + ms;
    fn(...args);
  };
}

// Wraps an async function so concurrent calls are dropped until it resolves.
// The re-entrancy guard for click/dblclick handlers that await before acting.
export function guard(fn) {
  let inflight = false;
  return async (...args) => {
    if (inflight) return;
    inflight = true;
    try { await fn(...args); } finally { inflight = false; }
  };
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

// Move-number prefix for a 0-based ply: "12." for white, "12..." for black.
export function fmtMoveNo(ply, isWhite) {
  return `${Math.floor(ply / 2) + 1}${isWhite ? "." : "..."}`;
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

// Selectable-region opt-in. The app root is `user-select: none` (app feel:
// Ctrl/Cmd+A on chrome selects nothing), and regions tagged here opt back
// in. installSelection() (called once) then scopes Ctrl/Cmd+A to the
// focused region and flattens row markup on copy. `rows` is a row selector
// for that flattening; `target(ev)` returns the node to select on Ctrl+A
// (default: the region itself); returning falsy leaves selection untouched.
const SELECTABLE_ATTR = "data-selectable";
const ROWS_ATTR = "data-rows";
export const isCtrlA = (ev) => (ev.ctrlKey || ev.metaKey) && (ev.key === "a" || ev.key === "A");

// Custom elements may host a shadow root, and tabindex=-1 on a shadow host
// drops that host's whole slotted subtree out of sequential focus navigation
// -- so they never get the marker tabindex. Name test, not `el.shadowRoot`:
// this can run before the element upgrades.
const isCustomElement = (el) => el.localName.includes("-");

export function markSelectable(el, { rows = null, target = null } = {}) {
  // Focusable by click/script (so Ctrl+A's keydown targets the region) but not
  // a Tab stop; respect an explicit tabindex if the element set one. Clicks
  // inside a skipped host still bubble to the region, so Ctrl+A keeps working.
  if (!el.hasAttribute("tabindex") && !isCustomElement(el)) el.tabIndex = -1;
  el.setAttribute(SELECTABLE_ATTR, "");
  if (rows) el.setAttribute(ROWS_ATTR, rows);
  if (target) el._selTarget = target;
}

// Roving tab stop: exactly one of `itemSel` inside `container` is tabbable,
// and each item's `controlSel` sub-buttons ride along with it. Without this a
// list of N items with C controls each costs N * (C + 1) Tab presses to cross.
const TAB_STOP_SEL = '[tabindex="0"]';

export function roveTabStop(container, itemSel, current, controlSel = []) {
  for (const item of container.querySelectorAll(itemSel)) {
    const on = item === current;
    item.tabIndex = on ? 0 : -1;
    for (const sel of controlSel) {
      const control = item.querySelector(sel);
      if (control) control.tabIndex = on ? 0 : -1;
    }
  }
}

// Re-seat the stop after a re-render: keep whichever item already holds it,
// else hand it to the first. Call whenever the item set changes.
export function syncRovingTabStop(container, itemSel, controlSel = []) {
  const items = container.querySelectorAll(itemSel);
  if (items.length === 0) return;
  const current = container.querySelector(itemSel + TAB_STOP_SEL) ?? items[0];
  roveTabStop(container, itemSel, current, controlSel);
}

// Promote a bare <span>/<div> acting as a button: screen readers see a button,
// and Enter/Space activate it the way a real one would. Idempotent, so it is
// safe to re-run over elements a renderer may or may not have replaced.
const KBD_WIRED_ATTR = "kbd";

export function wireSpanButton(el, label) {
  if (!el || el.dataset[KBD_WIRED_ATTR]) return;
  el.dataset[KBD_WIRED_ATTR] = "1";
  el.setAttribute("role", "button");
  el.setAttribute("aria-label", label);
  el.tabIndex = -1;
  el.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    ev.preventDefault();
    el.click();
  });
}

// Single-tab-stop navigation: the container is one Tab stop and the arrows
// move the selection between its `rows`. `select(row, i)` owns what selecting
// means -- the helper only picks the next row and scrolls it into view, so
// each caller keeps its own selection bookkeeping.
//
// `cols()` opts into grid geometry: Up/Down then jump a whole row and
// Left/Right step one cell. Omit it for a plain list, where Up/Down step one
// row and Left/Right are left to the browser. `onEdge(row, i)` fires when a
// move is blocked at an edge, for callers that need to signal the bump.
// `scroll(row)` defaults to the sticky-header-aware row scroller; callers that
// scroll their own way (e.g. WinBox boards) pass their own or a no-op.
const LIST_NAV_KEYS = new Set(["ArrowDown", "ArrowUp", "Home", "End"]);
const GRID_NAV_KEYS = new Set([...LIST_NAV_KEYS, "ArrowLeft", "ArrowRight"]);

// Index the key moves to, or `cur` to stay put. A row jump landing outside the
// grid is a no-op: clamping it to the last cell would silently change column.
function nextNavIndex(key, cur, len, step) {
  if (key === "Home") return 0;
  if (key === "End") return len - 1;
  if (key === "ArrowRight") return cur < 0 ? 0 : Math.min(cur + 1, len - 1);
  if (key === "ArrowLeft") return cur < 0 ? len - 1 : Math.max(cur - 1, 0);
  const dir = key === "ArrowDown" ? 1 : -1;
  if (cur < 0) return dir > 0 ? 0 : len - 1;
  if (step === 1) return Math.min(Math.max(cur + dir, 0), len - 1);
  const target = cur + dir * step;
  return target >= 0 && target < len ? target : cur;
}

// `lead` names the row arrows move from when it isn't the selected one (a
// multi-selection walks a lead row); `extend` handles Shift+Arrow, which
// grows a selection instead of moving it.
export function wireArrowKeyNav(el, {
  rows, selected, select, cols = null, onEdge = null, scroll = scrollRowIntoView,
  lead = null, extend = null,
}) {
  // A list region's focus ring is suppressed, so keyboard focus would land
  // here with nothing to show for it -- highlight the first row instead.
  // :focus-visible keeps a mouse click (e.g. on a sort header) from selecting.
  el.addEventListener("focus", () => {
    if (!el.matches(":focus-visible") || el.querySelector(selected)) return;
    const first = el.querySelector(rows);
    if (first) select(first, 0);
  });

  el.addEventListener("keydown", (ev) => {
    if (!(cols ? GRID_NAV_KEYS : LIST_NAV_KEYS).has(ev.key)) return;
    if (ev.ctrlKey || ev.altKey || ev.metaKey) return;
    const all = Array.from(el.querySelectorAll(rows));
    if (all.length === 0) return;
    // Claim the key even when the move is a no-op at an edge, so the region
    // doesn't scroll out from under a selection that stayed put.
    ev.preventDefault();
    const cur = all.indexOf(lead?.() || el.querySelector(selected));
    const next = nextNavIndex(ev.key, cur, all.length, cols ? Math.max(1, cols()) : 1);
    // Blocked at an edge. Callers with no persistent selection ring use onEdge
    // to say "still here"; a plain list just stays put.
    if (next === cur) {
      if (cur >= 0) onEdge?.(all[cur], cur);
      return;
    }
    // Scroll before select: a select() that re-renders the list detaches this
    // row, and scrolling a detached node does nothing. The rebuilt row lands
    // at the same index, so the scroll still lines up.
    scroll(all[next]);
    const apply = ev.shiftKey && extend ? extend : select;
    apply(all[next], next);
  });
}

// A markSelectable region's rows are `user-select: text`, so a double/triple
// click on a row that drives an action (select/open/commit) paints a word or
// paragraph selection first. Attach to the list container to cancel the
// multi-click text gesture; single-click, dblclick, and drag-select still work.
export function suppressMultiClickSelect(el) {
  el.addEventListener("mousedown", (ev) => { if (ev.detail > 1) ev.preventDefault(); });
}

// A Shift/Ctrl+click otherwise paints a text selection across the region on
// top of the row gesture it drives. Cancel the text-drag -- but preventDefault
// also drops focus, so refocus the region or its keyboard nav goes dead.
export function suppressModifierClickSelect(el) {
  el.addEventListener("mousedown", (ev) => {
    if (!ev.shiftKey && !ev.ctrlKey && !ev.metaKey) return;
    ev.preventDefault();
    el.focus();
  });
}

const closestRegion = (node) => {
  const el = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
  return el?.closest(`[${SELECTABLE_ATTR}]`) || null;
};

// Drop the active region selection unless `keep` lies inside its region
// (keep == null always drops). Shared by Escape and outside-click.
const clearOutside = (keep) => {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return;
  const region = closestRegion(sel.anchorNode);
  if (region && !(keep && region.contains(keep))) sel.removeAllRanges();
};

// True when `el` (a keydown target) is, or shadow-delegates focus into, a
// text-entry control whose native Ctrl/Cmd+A must be left alone. WA form
// controls wrap a native input in shadow DOM and retarget the event to the
// host, so follow the focused element down through shadow roots.
const isEditableTarget = (el) => {
  for (let n = el; n; n = n.shadowRoot?.activeElement) {
    if (n.isContentEditable || n.tagName === "INPUT" || n.tagName === "TEXTAREA") return true;
  }
  return false;
};

// Collapse one flex/grid row to a single text line: join its child elements'
// text with a space, skipping interactive (button) and decorative
// (aria-hidden) children. Each child sits on one visual line but blockifies to
// its own line in the default copy serialization; this undoes that.
function rowToLine(row) {
  return Array.from(row.children)
    .filter((c) => c.tagName !== "BUTTON" && c.getAttribute("aria-hidden") !== "true")
    .map((c) => c.textContent.trim())
    .filter(Boolean)
    .join(" ");
}

// Install once. Ctrl/Cmd+A inside a selectable region selects that region's
// contents (or its target node) instead of the page. On copy, a region with
// a `rows` selector serializes each row as one line via rowToLine,
// overriding the browser's per-flex/grid-child line breaks. (Real <table>s
// copy one-row-per-line natively, so they omit `rows`.) Only innermost row
// matches are serialized so a nested list can't duplicate its parent row.
export function installSelection() {
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { clearOutside(null); return; }
    if (!isCtrlA(ev)) return;
    const region = closestRegion(ev.target);
    if (!region) {
      // Suppress the native page-wide select-all so nothing ever flashes;
      // leave a text field's own select-all alone.
      if (!isEditableTarget(ev.target)) ev.preventDefault();
      return;
    }
    ev.preventDefault();
    const node = region._selTarget ? region._selTarget(ev) : region;
    if (!node) return;
    const sel = window.getSelection();
    sel.removeAllRanges();
    const range = document.createRange();
    range.selectNodeContents(node);
    sel.addRange(range);
  }, true);

  // A click outside the region (or Escape, above) clears its selection so it
  // never lingers as a grey inactive highlight. Keyed off mousedown, not
  // focus: a winbox titlebar mousedown preventDefaults and keeps focus on the
  // region, so focus-based clearing misses it. Capture beats stopPropagation.
  document.addEventListener("mousedown", (ev) => clearOutside(ev.target), true);

  document.addEventListener("copy", (ev) => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return;
    const region = closestRegion(sel.anchorNode);
    const rowSelector = region?.getAttribute(ROWS_ATTR);
    if (!rowSelector || !region.contains(sel.focusNode)) return;
    const rows = Array.from(sel.getRangeAt(0).cloneContents().querySelectorAll(rowSelector))
      .filter((r) => !r.querySelector(rowSelector));
    if (!rows.length) return;
    ev.clipboardData.setData("text/plain", rows.map(rowToLine).filter(Boolean).join("\n"));
    ev.preventDefault();
  });
}
