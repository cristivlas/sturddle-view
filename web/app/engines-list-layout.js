// Pure-DOM layout helpers for the Settings > Engines list. No list/CRUD
// state -- only measurement, column resize, and the ResizeObserver wiring.
// Split out of mountEngineList so the controller core stays under the cap.

import { attachColumnResize, makePctApplySizes } from "./col-resize.js";
import { rafCoalesce } from "./wb-utils.js";

const DEFAULT_PCTS = [20, 14, 66];
const MIN_COL_PCT = 8;
// Floor for the scrollable list height + slack subtracted from the
// measured body so the last row clears the ribbon.
const MIN_WRAP_PX = 120;
const WRAP_SLACK_PX = 8;

export function attachEngineColResize(container, colPctsKey) {
  const headTableEl = container.querySelector(".engines-head-table");
  const bodyTableEl = container.querySelector(".engines-body-table");
  const headColEls = Array.from(headTableEl.querySelectorAll("col"));
  const bodyColEls = Array.from(bodyTableEl.querySelectorAll("col"));
  const wrapEl = container.querySelector(".engines-table-wrap");
  const grips = Array.from(headTableEl.querySelectorAll(".th-grip"));
  const colPcts = DEFAULT_PCTS.slice();

  attachColumnResize({
    table: headTableEl,
    grips,
    overlayHost: wrapEl,
    storageKey: colPctsKey,
    sizes: colPcts,
    unit: "pct",
    applySizes: makePctApplySizes([headColEls, bodyColEls], MIN_COL_PCT),
  });
}

// Sizes the table-wrap so the LIST scrolls internally instead of the dialog
// body. Returns { sizeWrap, scheduleSizeWrap, teardown }: the factory calls
// sizeWrap once, then teardown on dialog close to drop listeners/observers.
export function createWrapSizer(container) {
  const wrapEl = container.querySelector(".engines-table-wrap");
  const bodyMainEl = container.querySelector(".engines-body-main");
  const ribbonEl = container.querySelector(".engines-ribbon");

  function sizeWrap() {
    const dialog = container.closest("wa-dialog");
    const body = dialog?.shadowRoot?.querySelector('[part~="body"]');
    const topAnchor = bodyMainEl.getBoundingClientRect().top;
    const bodyBottom = body
      ? body.getBoundingClientRect().bottom
      : window.innerHeight - WRAP_SLACK_PX;
    const ribbonH = ribbonEl?.offsetHeight || 0;
    let h = Math.max(MIN_WRAP_PX, Math.floor(bodyBottom - topAnchor - ribbonH - WRAP_SLACK_PX));
    wrapEl.style.height = h + "px";
    // Expose ribbon height so the absolute search-wrap can anchor above it.
    container.style.setProperty("--engines-ribbon-h", ribbonH + "px");
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

  const dialog = container.closest("wa-dialog");
  const tabGroup = container.closest("wa-tab-group");
  const ro = (typeof ResizeObserver !== "undefined" && dialog)
    ? new ResizeObserver(scheduleSizeWrap) : null;
  if (ro) {
    ro.observe(dialog);
    // Observe bodyMain so future panel content changes (added rows,
    // font-size shifts) trigger a re-measure automatically. sizeWrap
    // only mutates a child (engines-table-wrap), so no feedback loop.
    ro.observe(bodyMainEl);
  }

  // Re-measure when switching back to the engines tab: other tabs are
  // content-sized so the body's scrollHeight may have changed.
  const onTabShow = (ev) => {
    if (ev.detail?.name === container.closest("wa-tab-panel")?.name) {
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
