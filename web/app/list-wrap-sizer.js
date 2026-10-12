// Sizes a list's scroll wrap to the space left in its dialog body, so the
// LIST scrolls internally and the dialog body never does. Shared by the
// Settings Engines and Recents tabs.

import { rafCoalesce } from "./wb-utils.js";

// Floor for the scrollable list height + slack subtracted from the
// measured body so the last row clears the ribbon.
const MIN_WRAP_PX = 120;
const WRAP_SLACK_PX = 8;

// `wrapEl` gets the pixel height; it fills from `anchorEl`'s top down to the
// dialog body's bottom, less `ribbonEl` (an optional bar below the list),
// whose height goes to `onRibbonHeight`. Returns { sizeWrap,
// scheduleSizeWrap, teardown }: call sizeWrap once, then teardown on dialog
// close to drop listeners/observers.
export function createWrapSizer({ wrapEl, anchorEl, ribbonEl = null, onRibbonHeight = null }) {
  function sizeWrap() {
    const dialog = wrapEl.closest("wa-dialog");
    const body = dialog?.shadowRoot?.querySelector('[part~="body"]');
    const topAnchor = anchorEl.getBoundingClientRect().top;
    const bodyBottom = body
      ? body.getBoundingClientRect().bottom
      : window.innerHeight - WRAP_SLACK_PX;
    const ribbonH = ribbonEl?.offsetHeight || 0;
    let h = Math.max(MIN_WRAP_PX, Math.floor(bodyBottom - topAnchor - ribbonH - WRAP_SLACK_PX));
    wrapEl.style.height = h + "px";
    onRibbonHeight?.(ribbonH);
    if (body) {
      const overflow = body.scrollHeight - body.clientHeight;
      if (overflow > 0) {
        h = Math.max(MIN_WRAP_PX, h - overflow);
        wrapEl.style.height = h + "px";
      }
    }
  }

  // Coalesce rapid resize bursts into one measurement per frame.
  const scheduleSizeWrap = rafCoalesce(sizeWrap);

  const dialog = wrapEl.closest("wa-dialog");
  const tabGroup = wrapEl.closest("wa-tab-group");
  const ro = (typeof ResizeObserver !== "undefined" && dialog)
    ? new ResizeObserver(scheduleSizeWrap) : null;
  if (ro) {
    ro.observe(dialog);
    // Observe the anchor so later content changes above the list (added
    // rows, font-size shifts) trigger a re-measure automatically.
    ro.observe(anchorEl);
  }

  // Re-measure when switching back to this tab: other tabs are
  // content-sized so the body's scrollHeight may have changed.
  const onTabShow = (ev) => {
    if (ev.detail?.name === wrapEl.closest("wa-tab-panel")?.name) {
      scheduleSizeWrap();
    }
  };
  if (tabGroup) tabGroup.addEventListener("wa-tab-show", onTabShow);

  window.addEventListener("resize", scheduleSizeWrap);

  function teardown() {
    scheduleSizeWrap.cancel();
    window.removeEventListener("resize", scheduleSizeWrap);
    if (tabGroup) tabGroup.removeEventListener("wa-tab-show", onTabShow);
    if (ro) ro.disconnect();
  }

  return { sizeWrap, scheduleSizeWrap, teardown };
}
